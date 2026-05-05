#!/usr/bin/env python3
"""
Lightspark Transaction Export
Auth: pre-captured LIGHTSPARK_SESSION (base64 Playwright storage state).
      Capture locally using capture_session.py (non-headless to bypass bot detection).
Flow: load session -> verify auth -> generate CSV (is between dates) -> poll Gmail -> download -> S3
"""

import argparse
import asyncio
import base64
import email as email_lib
import imaplib
import json
import os
import re
import time
import boto3
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# === CONFIG ===
REPORTS_URL = "https://app.lightspark.com/reports"
DOWNLOAD_DIR = Path("downloads")

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")

S3_BUCKET = os.getenv("S3_BUCKET", "payout-recon")
S3_PREFIX = os.getenv("LIGHTSPARK_S3_PREFIX", "lightspark/raw/")


def load_session():
    data = os.getenv("LIGHTSPARK_SESSION")
    if not data:
        raise Exception(
            "LIGHTSPARK_SESSION secret is not set. "
            "Run capture_session.py locally and add the output to GitHub Secrets."
        )
    try:
        return json.loads(base64.b64decode(data.strip()).decode())
    except Exception as e:
        raise Exception(f"Failed to decode LIGHTSPARK_SESSION: {e}")


async def screenshot(page, name):
    try:
        await page.screenshot(path=f"ls_dbg_{name}.png", full_page=False)
        print(f"[debug] screenshot: ls_dbg_{name}.png")
    except Exception as e:
        print(f"[debug] screenshot failed: {e}")


# === AUTH CHECK ===
async def ensure_auth(page):
    print("[auth] Checking session...")
    await page.goto(REPORTS_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    await screenshot(page, "01_auth_check")
    if "login" in page.url:
        raise Exception(
            "Session expired. Run capture_session.py locally again and update LIGHTSPARK_SESSION."
        )
    print(f"[auth] Session valid — {page.url}")


# === REPORT ===
async def generate_report(page, start_date: str, end_date: str):
    """
    start_date / end_date: YYYY-MM-DD
    Modal flow: click Custom -> set filter to "is between" -> fill 6 number inputs -> Generate CSV
    """
    print(f"[report] Requesting CSV for {start_date} -> {end_date}")

    # Parse dates
    s_year, s_month, s_day = start_date.split("-")
    e_year, e_month, e_day = end_date.split("-")

    await page.goto(REPORTS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    await screenshot(page, "02_reports")

    # Open the generate report modal
    btn = page.locator("button:has-text('Generate transaction report')")
    await btn.first.click()
    await page.wait_for_timeout(2000)
    await screenshot(page, "03_modal_opened")

    # Click Custom
    custom_btn = page.locator("button:has-text('Custom')")
    await custom_btn.first.click()
    await page.wait_for_timeout(1000)
    await screenshot(page, "04_custom_selected")

    # Select "is between" from the React Select combobox
    # The combobox is type=text; type to filter, then ArrowDown+Enter to select
    combobox = page.locator("input[type='text']").first
    await combobox.fill("is between")
    await page.wait_for_timeout(500)
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1000)
    await screenshot(page, "05_is_between")

    # Fill the 6 number inputs: MM DD YYYY (start) then MM DD YYYY (end)
    date_vals = [s_month, s_day, s_year, e_month, e_day, e_year]
    await page.evaluate(
        """(vals) => {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            const inputs = document.querySelectorAll('input[type="number"]');
            vals.forEach((v, i) => {
                if (inputs[i]) {
                    setter.call(inputs[i], v);
                    inputs[i].dispatchEvent(new Event('input',  { bubbles: true }));
                    inputs[i].dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
        }""",
        date_vals,
    )
    await page.wait_for_timeout(500)
    await screenshot(page, "06_dates_set")

    # Click Generate CSV
    gen_btn = page.locator("button:has-text('Generate CSV')")
    await gen_btn.first.click()
    print("[report] CSV generation requested")
    await page.wait_for_timeout(2000)
    await screenshot(page, "07_csv_requested")


# === GMAIL POLLING ===
def poll_email():
    """Poll Gmail specifically for Lightspark report emails.
    Searches FROM @lightspark.com addresses only to avoid false positives from
    GitHub notifications about the Lightspark repo.
    """
    import datetime
    print("[gmail] Polling inbox for Lightspark download link...")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_EMAIL, GMAIL_PASS)
    imap.select("inbox")

    today = datetime.date.today().strftime("%d-%b-%Y")

    # Search only for actual Lightspark product emails (not GitHub notifications)
    searches = [
        f'FROM "lightspark.com" SINCE {today}',
        f'FROM "lightspark.com"',
    ]

    # Also accept forwarded emails — check BODY for lightspark download URLs directly
    fallback_searches = [
        f'BODY "lightspark.com" SINCE {today}',
    ]

    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp")

    for attempt in range(30):
        all_searches = searches if attempt < 5 else searches + fallback_searches
        for search_query in all_searches:
            try:
                _, msgs = imap.search(None, search_query)
            except Exception:
                continue
            ids = msgs[0].split()
            if not ids:
                continue

            for mid in reversed(ids[-10:]):
                try:
                    _, data = imap.fetch(mid, "(RFC822)")
                    msg = email_lib.message_from_bytes(data[0][1])
                    sender = msg.get("From", "")
                    subject = msg.get("Subject", "")
                    print(f"[gmail] Checking: FROM={sender[:60]} SUBJECT={subject[:60]}")

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ("text/html", "text/plain"):
                                body += part.get_payload(decode=True).decode(errors="replace")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="replace")

                    all_urls = re.findall(r'https?://[^\s"<>\]]+', body)
                    # Filter out images and non-lightspark URLs
                    candidate_urls = [
                        u for u in all_urls
                        if "lightspark" in u.lower()
                        and not any(u.lower().endswith(ext) for ext in IMAGE_EXTS)
                    ]
                    if candidate_urls:
                        chosen = candidate_urls[0]
                        print(f"[gmail] Found Lightspark URL on attempt {attempt + 1}: {chosen[:100]}")
                        imap.logout()
                        return chosen
                except Exception as e:
                    print(f"[gmail] Error reading message: {e}")

        print(f"[gmail] Attempt {attempt + 1}/30 — no link yet, waiting 10s...")
        time.sleep(10)

    imap.logout()
    raise Exception("[gmail] No download link found after 5 minutes")


# === DOWNLOAD ===
async def download_file(page, url, filename):
    print(f"[download] Navigating to: {url}")
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    try:
        await page.goto(url, wait_until="domcontentloaded")
        btn = page.locator("button:has-text('Download'), a:has-text('Download')")
        async with page.expect_download(timeout=60000) as dl:
            await btn.first.click()
        download = await dl.value
    except Exception:
        async with page.expect_download(timeout=60000) as dl:
            await page.goto(url)
        download = await dl.value

    path = DOWNLOAD_DIR / filename
    await download.save_as(path)
    print(f"[download] Saved: {path}")
    return path


# === S3 ===
def upload_s3(path):
    s3 = boto3.client("s3")
    key = f"{S3_PREFIX}{path.name}"
    s3.upload_file(str(path), S3_BUCKET, key)
    uri = f"s3://{S3_BUCKET}/{key}"
    print(f"[s3] Uploaded: {uri}")
    return uri


# === MAIN ===
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end_date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    filename = f"LIGHTSPARK_{args.start_date}_to_{args.end_date}.csv"
    session_data = load_session()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            storage_state=session_data,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        await ensure_auth(page)
        await generate_report(page, args.start_date, args.end_date)

        url = poll_email()
        path = await download_file(page, url, filename)
        upload_s3(path)

        print("[DONE]")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())