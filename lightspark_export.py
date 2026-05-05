#!/usr/bin/env python3
"""
Lightspark Transaction Export
Auth: uses pre-captured LIGHTSPARK_SESSION (base64 Playwright storage state).
      Session is captured locally (non-headless) to bypass bot detection, then stored in GitHub Secrets.
Flow: load session -> verify auth -> generate CSV -> poll Gmail -> download -> S3
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


# === SESSION LOADER ===
def load_session():
    data = os.getenv("LIGHTSPARK_SESSION")
    if not data:
        raise Exception("LIGHTSPARK_SESSION secret is not set. Run capture_session.py locally and add the output to GitHub Secrets.")
    try:
        decoded = base64.b64decode(data.strip()).decode()
        return json.loads(decoded)
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
            "Session expired or invalid. Run capture_session.py locally again and update the LIGHTSPARK_SESSION secret."
        )
    print(f"[auth] Session valid — URL: {page.url}")


# === REPORT ===
async def generate_report(page, start, end):
    print(f"[report] Requesting CSV for {start} -> {end}")
    await page.goto(REPORTS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    await screenshot(page, "02_reports")

    btn = page.locator("text=Generate transaction report")
    await btn.first.click()
    await page.wait_for_timeout(3000)
    await screenshot(page, "03_modal")

    inputs = page.locator("input[type='text']")
    await inputs.nth(0).fill(start)
    await page.keyboard.press("Tab")
    await inputs.nth(1).fill(end)
    await screenshot(page, "04_dates")

    gen = page.locator("button:has-text('Generate CSV')")
    await gen.first.click()
    print("[report] CSV generation requested")
    await screenshot(page, "05_csv_requested")


# === GMAIL POLLING ===
def poll_email():
    print("[gmail] Polling inbox for Lightspark download link...")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_EMAIL, GMAIL_PASS)
    imap.select("inbox")

    for attempt in range(30):
        _, msgs = imap.search(None, 'FROM "lightspark"')
        ids = msgs[0].split()

        for mid in reversed(ids[-10:]):
            _, data = imap.fetch(mid, "(RFC822)")
            msg = email_lib.message_from_bytes(data[0][1])

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() in ("text/html", "text/plain"):
                        body += part.get_payload(decode=True).decode(errors="replace")
            else:
                body = msg.get_payload(decode=True).decode(errors="replace")

            urls = re.findall(r'https?://[^\s"<>]+', body)
            download_urls = [
                u for u in urls
                if "download" in u.lower() or "csv" in u.lower() or "lightspark" in u.lower()
            ]
            if download_urls:
                print(f"[gmail] Found download link on attempt {attempt + 1}")
                imap.logout()
                return download_urls[0]

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
        btn = page.locator("button:has-text('Download'), a:has-text('Download'), text=Download")
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