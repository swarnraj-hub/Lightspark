#!/usr/bin/env python3
"""
Lightspark Transaction Export — Full Login + CSV Generation + S3 Upload
Auth  : email + password + TOTP (runs non-headless via xvfb in CI)
Flow  : login -> generate CSV (Custom / is between) -> poll Gmail -> download -> S3
"""

import argparse
import asyncio
import email as email_lib
import imaplib
import os
import re
import time
import boto3
import pyotp
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
LIGHTSPARK_LOGIN_URL = "https://app.lightspark.com/login"
REPORTS_URL          = "https://app.lightspark.com/reports"
DOWNLOAD_DIR         = Path("downloads")

LIGHTSPARK_EMAIL    = os.getenv("LIGHTSPARK_EMAIL")
LIGHTSPARK_PASSWORD = os.getenv("LIGHTSPARK_PASSWORD")
TOTP_SECRET         = os.getenv("LIGHTSPARK_TOTP_SECRET")

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASS  = os.getenv("GMAIL_APP_PASSWORD")

S3_BUCKET = os.getenv("S3_BUCKET", "payout-recon")
S3_PREFIX = os.getenv("LIGHTSPARK_S3_PREFIX", "lightspark/raw/")


# ── Helpers ───────────────────────────────────────────────────────────────────
async def screenshot(page, name):
    try:
        await page.screenshot(path=f"ls_dbg_{name}.png", full_page=False)
        print(f"[debug] {name}.png")
    except Exception as e:
        print(f"[debug] screenshot failed: {e}")


# ── Login ─────────────────────────────────────────────────────────────────────
async def do_login(page):
    print("[login] Navigating to login page...")
    await page.goto(LIGHTSPARK_LOGIN_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)
    await screenshot(page, "01_login")

    # Step 1: click "Continue with email" -> /login/email
    try:
        await page.wait_for_selector(
            'a[href="/login/email"], button:has-text("Continue with email")',
            timeout=10000, state="visible"
        )
        await page.locator(
            'a[href="/login/email"], button:has-text("Continue with email")'
        ).first.click()
        await page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        if "login/email" not in page.url:
            await page.goto("https://app.lightspark.com/login/email",
                            wait_until="networkidle")
            await page.wait_for_timeout(2000)
    await screenshot(page, "02_email_form")

    # Step 2: fill email
    await page.wait_for_selector(
        'input[placeholder="Email"], input[type="email"]',
        timeout=15000, state="visible"
    )
    await page.locator(
        'input[placeholder="Email"], input[type="email"]'
    ).first.fill(LIGHTSPARK_EMAIL)
    print("[login] Email filled")

    # Step 3: fill password
    await page.wait_for_selector(
        'input[placeholder="Password"], input[type="password"]',
        timeout=10000, state="visible"
    )
    await page.locator(
        'input[placeholder="Password"], input[type="password"]'
    ).first.fill(LIGHTSPARK_PASSWORD)
    print("[login] Password filled")
    await screenshot(page, "03_credentials")

    # Step 4: submit
    await page.locator(
        'button[type="submit"], button:has-text("Continue with email")'
    ).first.click()
    await page.wait_for_timeout(4000)
    await screenshot(page, "04_after_submit")

    # Step 5: TOTP dialog (6 individual number boxes)
    totp_selectors = [
        'input[aria-label*="Code input 1"]',
        'input[maxlength="1"][type="number"]',
        'input[maxlength="6"]',
        'input[autocomplete="one-time-code"]',
        'input[placeholder*="code" i]',
    ]
    totp_found = False
    for sel in totp_selectors:
        try:
            await page.wait_for_selector(sel, timeout=10000, state="visible")
            totp_found = True
            code = pyotp.TOTP(TOTP_SECRET).now()
            print(f"[login] TOTP: {code}")
            await page.locator(sel).first.click()
            await page.keyboard.type(code)
            break
        except PlaywrightTimeout:
            continue

    if totp_found:
        await page.wait_for_timeout(2000)
        await screenshot(page, "05_totp")
        try:
            await page.locator(
                'button:has-text("Continue"), button[type="submit"]'
            ).first.click(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)

        # Retry if TOTP expired
        if "login" in page.url:
            print("[login] TOTP may have expired — waiting for next window...")
            time.sleep(31)
            code = pyotp.TOTP(TOTP_SECRET).now()
            print(f"[login] Retry TOTP: {code}")
            for sel in totp_selectors:
                try:
                    await page.locator(sel).first.click(timeout=3000)
                    await page.keyboard.type(code)
                    break
                except Exception:
                    continue
            try:
                await page.locator(
                    'button:has-text("Continue"), button[type="submit"]'
                ).first.click(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
    else:
        print("[login] No TOTP screen — proceeding")

    await page.wait_for_load_state("networkidle", timeout=30000)
    await screenshot(page, "06_post_login")

    if "login" in page.url:
        raise Exception(f"[login] Login failed — still on: {page.url}")
    print(f"[login] Logged in — {page.url}")


# ── Generate Report ───────────────────────────────────────────────────────────
async def generate_report(page, start_date: str, end_date: str):
    """
    start_date / end_date: YYYY-MM-DD
    Opens the modal, selects Custom -> is between, fills dates, clicks Generate CSV.
    """
    print(f"[report] Requesting CSV: {start_date} -> {end_date}")
    s_year, s_month, s_day = start_date.split("-")
    e_year, e_month, e_day = end_date.split("-")

    await page.goto(REPORTS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    await screenshot(page, "07_reports")

    # Open modal
    await page.locator("button:has-text('Generate transaction report')").first.click()
    await page.wait_for_timeout(2000)
    await screenshot(page, "08_modal")

    # Click Custom
    await page.locator("button:has-text('Custom')").first.click()
    await page.wait_for_timeout(1000)
    await screenshot(page, "09_custom")

    # Select "is between" from React Select combobox
    combobox = page.locator("input[type='text']").first
    await combobox.click()
    await page.wait_for_timeout(300)
    await page.keyboard.type("is between", delay=60)
    await page.wait_for_timeout(500)
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1000)
    await screenshot(page, "10_is_between")

    # Fill the 6 date number inputs via native React setter
    # Order: MM DD YYYY (start)  MM DD YYYY (end)
    date_vals = [s_month, s_day, s_year, e_month, e_day, e_year]
    await page.evaluate(
        """(vals) => {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            const inputs = document.querySelectorAll('input[type="number"]');
            vals.forEach((v, i) => {
                if (!inputs[i]) return;
                setter.call(inputs[i], v);
                inputs[i].dispatchEvent(new Event('input',  { bubbles: true }));
                inputs[i].dispatchEvent(new Event('change', { bubbles: true }));
            });
        }""",
        date_vals,
    )
    await page.wait_for_timeout(500)
    await screenshot(page, "11_dates")

    # Click Generate CSV
    await page.locator("button:has-text('Generate CSV')").first.click()
    print("[report] CSV generation requested")
    await page.wait_for_timeout(2000)
    await screenshot(page, "12_submitted")


# ── Gmail Poll ────────────────────────────────────────────────────────────────
def poll_email():
    """
    Searches all of today's emails for any lightspark.com URL (not an image).
    Works regardless of which sending address Lightspark uses and whether the
    email is forwarded.
    """
    import datetime
    print("[gmail] Polling inbox for Lightspark download link...")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_EMAIL, GMAIL_PASS)
    imap.select("inbox")

    today = datetime.date.today().strftime("%d-%b-%Y")
    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".pdf")

    for attempt in range(60):
        for search_q in [f"SINCE {today}", "ALL"]:
            try:
                _, msgs = imap.search(None, search_q)
            except Exception:
                continue
            ids = msgs[0].split()
            if not ids:
                continue

            for mid in reversed(ids[-20:]):
                try:
                    _, data = imap.fetch(mid, "(RFC822)")
                    msg = email_lib.message_from_bytes(data[0][1])

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ("text/html", "text/plain"):
                                body += part.get_payload(decode=True).decode(errors="replace")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="replace")

                    candidates = [
                        u for u in re.findall(r'https?://[^\s"<>\]]+', body)
                        if "lightspark.com" in u.lower()
                        and not any(u.lower().endswith(ext) for ext in IMAGE_EXTS)
                        and len(u) > 40
                    ]
                    if candidates:
                        chosen = candidates[0]
                        print(f"[gmail] Found on attempt {attempt+1}")
                        print(f"[gmail] URL: {chosen[:120]}")
                        imap.logout()
                        return chosen
                except Exception as e:
                    print(f"[gmail] message error: {e}")
            break  # SINCE query found emails — skip ALL fallback

        print(f"[gmail] Attempt {attempt+1}/60 — waiting 10s...")
        time.sleep(10)

    imap.logout()
    raise Exception("[gmail] No Lightspark download link found after 10 minutes")


# ── Download ──────────────────────────────────────────────────────────────────
async def download_file(page, url, filename):
    print(f"[download] {url}")
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    try:
        await page.goto(url, wait_until="domcontentloaded")
        btn = page.locator(
            "button:has-text('Download'), a:has-text('Download'), text=Download"
        )
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


# ── S3 Upload ─────────────────────────────────────────────────────────────────
def upload_s3(path):
    s3  = boto3.client("s3")
    key = f"{S3_PREFIX}{path.name}"
    s3.upload_file(str(path), S3_BUCKET, key)
    uri = f"s3://{S3_BUCKET}/{key}"
    print(f"[s3] Uploaded: {uri}")
    return uri


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end_date",   required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    filename = f"LIGHTSPARK_{args.start_date}_to_{args.end_date}.csv"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,   # real browser on xvfb virtual display — bypasses bot detection
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()

        await do_login(page)
        await generate_report(page, args.start_date, args.end_date)

        url  = poll_email()
        path = await download_file(page, url, filename)
        upload_s3(path)

        print("[DONE]")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())