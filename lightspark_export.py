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

# Webshare rotating proxy
PROXY_SERVER   = os.getenv("PROXY_SERVER",   "")
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")


# ── Helpers ───────────────────────────────────────────────────────────────────
async def screenshot(page, name):
    try:
        await page.screenshot(path=f"ls_dbg_{name}.png", full_page=False)
        print(f"[debug] {name}.png")
    except Exception as e:
        print(f"[debug] screenshot failed: {e}")


# ── Login ─────────────────────────────────────────────────────────────────────
async def do_login(page):
    if not LIGHTSPARK_EMAIL or not LIGHTSPARK_PASSWORD or not TOTP_SECRET:
        raise RuntimeError(
            "[login] LIGHTSPARK_EMAIL / LIGHTSPARK_PASSWORD / LIGHTSPARK_TOTP_SECRET "
            "env vars are not set."
        )

    print("[login] Navigating to login page...")
    await page.goto(LIGHTSPARK_LOGIN_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)
    await screenshot(page, "01_login")
    print(f"[login] URL: {page.url}")

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
    print(f"[login] URL at form: {page.url}")

    # Step 2: fill email — use keyboard.type() so React controlled input fires onChange
    email_sel = 'input[placeholder="Email"], input[type="email"]'
    await page.wait_for_selector(email_sel, timeout=15000, state="visible")
    await page.locator(email_sel).first.click()
    await page.wait_for_timeout(300)
    await page.keyboard.press("Control+a")
    await page.keyboard.type(LIGHTSPARK_EMAIL, delay=40)
    # Verify React state picked it up
    filled_email = await page.locator(email_sel).first.input_value()
    print(f"[login] Email typed: {filled_email[:4]}*** (len={len(filled_email)})")

    # Step 3: fill password
    pass_sel = 'input[placeholder="Password"], input[type="password"]'
    await page.wait_for_selector(pass_sel, timeout=10000, state="visible")
    await page.locator(pass_sel).first.click()
    await page.wait_for_timeout(300)
    await page.keyboard.press("Control+a")
    await page.keyboard.type(LIGHTSPARK_PASSWORD, delay=40)
    filled_pass = await page.locator(pass_sel).first.input_value()
    print(f"[login] Password typed: {'*' * len(filled_pass)} (len={len(filled_pass)})")
    await screenshot(page, "03_credentials")

    # Step 4: submit via Enter (most reliable with React forms)
    await page.keyboard.press("Enter")
    print("[login] Enter pressed — waiting for navigation...")
    try:
        await page.wait_for_url(
            lambda url: "/login/email" not in url,
            timeout=15000
        )
        print(f"[login] Navigated to: {page.url}")
    except PlaywrightTimeout:
        body = (await page.inner_text("body"))[:600]
        print(f"[login] WARNING: still on /login/email after 15s")
        print(f"[login] Page text: {body}")
        await screenshot(page, "04_submit_stuck")

    await screenshot(page, "04_after_submit")
    print(f"[login] URL after submit: {page.url}")
    body_after = (await page.inner_text("body"))[:500]
    print(f"[login] Page snippet: {body_after}")

    # Bot detection check
    body_lower = body_after.lower()
    if "bot" in body_lower and ("behaviour" in body_lower or "behavior" in body_lower or "detected" in body_lower):
        print("[login] Bot detection page — waiting 15s...")
        await page.wait_for_timeout(15000)
        await screenshot(page, "04b_bot_detected")

    # Step 5: TOTP — wait for any digit input to appear (up to 20s)
    totp_combined = (
        'input[aria-label*="Code input"], '
        'input[maxlength="1"][type="number"], '
        'input[maxlength="6"], '
        'input[autocomplete="one-time-code"]'
    )
    totp_found = False
    try:
        await page.wait_for_selector(totp_combined, timeout=20000, state="visible")
        totp_found = True
    except PlaywrightTimeout:
        print("[login] No TOTP input after 20s")

    if totp_found:
        code = pyotp.TOTP(TOTP_SECRET).now()
        print(f"[login] TOTP: {code}")
        await screenshot(page, "05_totp")

        # 6 individual digit boxes?
        digit_boxes = page.locator('input[maxlength="1"][type="number"]')
        n_digits = await digit_boxes.count()
        if n_digits >= 6:
            await digit_boxes.nth(0).click()
            for ch in code:
                await page.keyboard.type(ch)
                await page.wait_for_timeout(80)
        else:
            await page.locator(totp_combined).first.click()
            await page.keyboard.type(code)

        await page.wait_for_timeout(2000)
        try:
            await page.locator(
                'button:has-text("Continue"), button[type="submit"]'
            ).first.click(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)

        # Retry if code expired
        if "login" in page.url:
            print("[login] TOTP rejected — waiting 31s for next code window...")
            time.sleep(31)
            code = pyotp.TOTP(TOTP_SECRET).now()
            print(f"[login] Retry TOTP: {code}")
            if n_digits >= 6:
                await digit_boxes.nth(0).click()
                for ch in code:
                    await page.keyboard.type(ch)
                    await page.wait_for_timeout(80)
            else:
                await page.locator(totp_combined).first.fill(code)
            try:
                await page.locator(
                    'button:has-text("Continue"), button[type="submit"]'
                ).first.click(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(5000)
    else:
        print("[login] Proceeding without TOTP")

    await page.wait_for_load_state("networkidle", timeout=30000)
    await screenshot(page, "06_post_login")
    print(f"[login] Final URL: {page.url}")

    if "login" in page.url:
        body = (await page.inner_text("body"))[:600]
        print(f"[login] Page at failure: {body}")
        raise Exception(f"[login] Login failed — still on: {page.url}")
    print(f"[login] Logged in — {page.url}")

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

    proxy_cfg = None
    if PROXY_SERVER:
        proxy_cfg = {"server": PROXY_SERVER}
        if PROXY_USERNAME:
            proxy_cfg["username"] = PROXY_USERNAME
            proxy_cfg["password"] = PROXY_PASSWORD
        print(f"[proxy] Using: {PROXY_SERVER}")
    else:
        print("[proxy] No proxy configured")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            proxy=proxy_cfg,
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