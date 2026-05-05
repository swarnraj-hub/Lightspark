#!/usr/bin/env python3
"""
Lightspark Transaction Export
Flow: login (email + password + TOTP) -> generate CSV -> poll Gmail -> download -> S3
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

# === CONFIG ===
LIGHTSPARK_LOGIN_URL = "https://app.lightspark.com/login"
REPORTS_URL = "https://app.lightspark.com/reports"
DOWNLOAD_DIR = Path("downloads")
DEBUG_DIR = Path(".")

LIGHTSPARK_EMAIL = os.getenv("LIGHTSPARK_EMAIL")
LIGHTSPARK_PASSWORD = os.getenv("LIGHTSPARK_PASSWORD")
TOTP_SECRET = os.getenv("LIGHTSPARK_TOTP_SECRET")

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")

S3_BUCKET = os.getenv("S3_BUCKET", "payout-recon")
S3_PREFIX = os.getenv("LIGHTSPARK_S3_PREFIX", "lightspark/raw/")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
}
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
    p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery(p);
"""


async def screenshot(page, name):
    try:
        path = str(DEBUG_DIR / f"ls_dbg_{name}.png")
        await page.screenshot(path=path, full_page=True)
        print(f"[debug] screenshot: {path}")
    except Exception as e:
        print(f"[debug] screenshot failed: {e}")


# === LOGIN ===
async def do_login(page):
    print("[login] Navigating to login page...")
    await page.goto(LIGHTSPARK_LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await screenshot(page, "01_login_page")

    # Try multiple email selectors with extended timeout
    email_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[placeholder*="email" i]',
        'input[autocomplete="email"]',
        'input[id*="email" i]',
    ]
    email_found = False
    for sel in email_selectors:
        try:
            await page.wait_for_selector(sel, timeout=8000, state="visible")
            await page.locator(sel).first.fill(LIGHTSPARK_EMAIL)
            email_found = True
            print(f"[login] Email filled via: {sel}")
            break
        except PlaywrightTimeout:
            continue

    if not email_found:
        await screenshot(page, "02_email_not_found")
        page_text = await page.evaluate("document.body.innerText")
        print(f"[login] Page text snippet: {page_text[:500]}")
        raise Exception("[login] Email input not found — possible bot detection or page change")

    await page.wait_for_timeout(1000)

    # Click Continue / Next
    try:
        btn = page.locator('button:has-text("Continue"), button:has-text("Next"), button[type="submit"]').first
        await btn.click(timeout=5000)
        await page.wait_for_timeout(3000)
        await screenshot(page, "03_after_continue")
    except Exception:
        pass

    # Password
    pwd_selectors = ['input[type="password"]', 'input[name="password"]']
    pwd_found = False
    for sel in pwd_selectors:
        try:
            await page.wait_for_selector(sel, timeout=10000, state="visible")
            await page.locator(sel).first.fill(LIGHTSPARK_PASSWORD)
            pwd_found = True
            print(f"[login] Password filled via: {sel}")
            break
        except PlaywrightTimeout:
            continue

    if not pwd_found:
        await screenshot(page, "04_password_not_found")
        raise Exception("[login] Password input not found")

    await screenshot(page, "05_credentials_filled")

    # Submit
    try:
        await page.locator(
            'button[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Login")'
        ).first.click(timeout=5000)
    except Exception:
        await page.keyboard.press("Enter")

    await page.wait_for_timeout(5000)
    await screenshot(page, "06_after_submit")

    # TOTP
    totp_selectors = [
        'input[placeholder*="code" i]',
        'input[maxlength="6"]',
        'input[autocomplete="one-time-code"]',
        'input[placeholder*="otp" i]',
        'input[type="number"][maxlength="1"]',
    ]
    totp_found = False
    for sel in totp_selectors:
        try:
            await page.wait_for_selector(sel, timeout=12000, state="visible")
            code = pyotp.TOTP(TOTP_SECRET).now()
            print(f"[login] TOTP code: {code}")
            await page.locator(sel).first.fill(code)
            totp_found = True
            print(f"[login] TOTP filled via: {sel}")
            break
        except PlaywrightTimeout:
            continue

    if totp_found:
        await screenshot(page, "07_totp_filled")
        try:
            await page.locator(
                'button[type="submit"], button:has-text("Verify"), button:has-text("Continue")'
            ).first.click(timeout=5000)
        except Exception:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)

        # Retry if expired
        if "login" in page.url or await page.locator("text=/invalid|expired|incorrect/i").count() > 0:
            print("[login] TOTP may have expired — waiting for next window...")
            time.sleep(31)
            code = pyotp.TOTP(TOTP_SECRET).now()
            print(f"[login] Retry TOTP: {code}")
            for sel in totp_selectors:
                try:
                    el = page.locator(sel).first
                    await el.fill(code, timeout=5000)
                    break
                except Exception:
                    continue
            try:
                await page.locator(
                    'button[type="submit"], button:has-text("Verify"), button:has-text("Continue")'
                ).first.click(timeout=5000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(4000)
    else:
        print("[login] No TOTP screen found — assuming direct login")

    await page.wait_for_load_state("networkidle", timeout=30000)
    await screenshot(page, "08_after_login")

    if "login" in page.url:
        raise Exception(f"[login] Login failed — still on login page: {page.url}")

    print(f"[login] Logged in — URL: {page.url}")


# === REPORT ===
async def generate_report(page, start, end):
    print(f"[report] Requesting CSV for {start} -> {end}")
    await page.goto(REPORTS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    await screenshot(page, "09_reports_page")

    btn = page.locator("text=Generate transaction report")
    await btn.first.click()
    await page.wait_for_timeout(3000)
    await screenshot(page, "10_report_modal")

    inputs = page.locator("input[type='text']")
    await inputs.nth(0).fill(start)
    await page.keyboard.press("Tab")
    await inputs.nth(1).fill(end)
    await screenshot(page, "11_dates_filled")

    gen = page.locator("button:has-text('Generate CSV')")
    await gen.first.click()
    print("[report] CSV generation requested")
    await screenshot(page, "12_csv_requested")


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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-plugins",
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
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        await do_login(page)
        await generate_report(page, args.start_date, args.end_date)

        url = poll_email()
        path = await download_file(page, url, filename)
        upload_s3(path)

        print("[DONE]")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())