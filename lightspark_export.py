#!/usr/bin/env python3
"""
Lightspark Transaction Export
Flow: /login -> click "Continue with email" -> /login/email (email+password) -> TOTP -> generate CSV -> poll Gmail -> download -> S3
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
        await page.screenshot(path=f"ls_dbg_{name}.png", full_page=False)
        print(f"[debug] screenshot: ls_dbg_{name}.png")
    except Exception as e:
        print(f"[debug] screenshot failed: {e}")


# === LOGIN ===
async def do_login(page):
    print("[login] Navigating to login page...")
    await page.goto(LIGHTSPARK_LOGIN_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)
    await screenshot(page, "01_login")

    # Step 1: Click "Continue with email" to reach /login/email
    try:
        await page.wait_for_selector(
            'a[href="/login/email"], button:has-text("Continue with email"), a:has-text("Continue with email")',
            timeout=10000, state="visible"
        )
        await page.locator(
            'a[href="/login/email"], button:has-text("Continue with email"), a:has-text("Continue with email")'
        ).first.click()
        await page.wait_for_timeout(3000)
        await screenshot(page, "02_email_form")
    except PlaywrightTimeout:
        # Maybe already on /login/email
        if "/login/email" not in page.url:
            await page.goto("https://app.lightspark.com/login/email", wait_until="networkidle")
            await page.wait_for_timeout(2000)

    # Step 2: Fill email
    await page.wait_for_selector('input[placeholder="Email"], input[type="email"]', timeout=15000, state="visible")
    await page.locator('input[placeholder="Email"], input[type="email"]').first.fill(LIGHTSPARK_EMAIL)
    print("[login] Email filled")

    # Step 3: Fill password
    await page.wait_for_selector('input[placeholder="Password"], input[type="password"]', timeout=10000, state="visible")
    await page.locator('input[placeholder="Password"], input[type="password"]').first.fill(LIGHTSPARK_PASSWORD)
    print("[login] Password filled")
    await screenshot(page, "03_credentials")

    # Step 4: Submit
    await page.locator('button:has-text("Continue with email"), button[type="submit"]').first.click()
    await page.wait_for_timeout(3000)
    await screenshot(page, "04_after_submit")

    # Step 5: TOTP dialog
    totp_selectors = [
        'input[aria-label*="Code input 1"]',
        'input[aria-label*="code" i]',
        'input[placeholder*="code" i]',
        'input[maxlength="1"][type="number"]',
        'input[maxlength="6"]',
        'input[autocomplete="one-time-code"]',
    ]
    totp_found = False
    for sel in totp_selectors:
        try:
            await page.wait_for_selector(sel, timeout=8000, state="visible")
            totp_found = True
            code = pyotp.TOTP(TOTP_SECRET).now()
            print(f"[login] TOTP code: {code}")
            # Type all 6 digits — they auto-advance between boxes
            await page.locator(sel).first.click()
            await page.keyboard.type(code)
            print(f"[login] TOTP entered via: {sel}")
            break
        except PlaywrightTimeout:
            continue

    if totp_found:
        await page.wait_for_timeout(2000)
        await screenshot(page, "05_totp")
        # Submit if there's a Continue button
        try:
            await page.locator('button:has-text("Continue"), button[type="submit"]').first.click(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)

        # Retry if TOTP expired
        if "login" in page.url:
            print("[login] TOTP may have expired — waiting for next window and retrying...")
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
                await page.locator('button:has-text("Continue"), button[type="submit"]').first.click(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
    else:
        print("[login] No TOTP screen — proceeding")

    await page.wait_for_load_state("networkidle", timeout=30000)
    await screenshot(page, "06_post_login")

    if "login" in page.url:
        raise Exception(f"[login] Login failed — still on login page: {page.url}")

    print(f"[login] Logged in — URL: {page.url}")


# === REPORT ===
async def generate_report(page, start, end):
    print(f"[report] Requesting CSV for {start} -> {end}")
    await page.goto(REPORTS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    await screenshot(page, "07_reports")

    btn = page.locator("text=Generate transaction report")
    await btn.first.click()
    await page.wait_for_timeout(3000)
    await screenshot(page, "08_modal")

    inputs = page.locator("input[type='text']")
    await inputs.nth(0).fill(start)
    await page.keyboard.press("Tab")
    await inputs.nth(1).fill(end)
    await screenshot(page, "09_dates")

    gen = page.locator("button:has-text('Generate CSV')")
    await gen.first.click()
    print("[report] CSV generation requested")


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