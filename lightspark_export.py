#!/usr/bin/env python3
"""
Lightspark - Automated Transaction Report Export -> S3 Upload -> Slack DM

Flow:
  1. Accept --start_date / --end_date (YYYY-MM-DD)
  2. Login to Lightspark (email + password + TOTP) [stealth mode]
  3. Navigate to Reports -> Generate transaction report
  4. Set custom date range, click Generate CSV
  5. Poll Gmail IMAP for the Lightspark download-link email
  6. Navigate to the download URL in Playwright (session already authenticated)
  7. Click Download button -> save file
  8. Upload to S3: s3://payout-recon/lightspark/raw/<filename>
  9. Send Slack DM

n8n calls this script as:
    python lightspark_export.py --start_date 2026-03-10 --end_date 2026-03-17
"""

import argparse
import asyncio
import base64
import email as email_lib
import imaplib
import json
import os
import random
import re
import time
import pyotp
import boto3
import requests
from botocore.exceptions import ClientError, NoCredentialsError
from datetime import datetime
from email.header import decode_header
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", type=str, default=None)
_parser.add_argument("--end_date",   type=str, default=None)
_args = _parser.parse_args()

_start_raw = _args.start_date or os.environ.get("EXPORT_START_DATE", "")
_end_raw   = _args.end_date   or os.environ.get("EXPORT_END_DATE",   "")

def _parse(date_str: str) -> datetime:
    return datetime.strptime(date_str.strip(), "%Y-%m-%d")

def to_lightspark_date(d: datetime) -> str:
    return f"{d.month}/{d.day}/{d.year}"

def to_file_date(d: datetime) -> str:
    return d.strftime("%d%m%Y")

START_DT = _parse(_start_raw)
END_DT   = _parse(_end_raw)
EXPORT_FILENAME = f"LIGHTSPARK_{to_file_date(START_DT)}_to_{to_file_date(END_DT)}.csv"

LS_EMAIL        = os.environ.get("LIGHTSPARK_EMAIL",       "")
LS_PASSWORD     = os.environ.get("LIGHTSPARK_PASSWORD",    "")
LS_TOTP_SECRET  = os.environ.get("LIGHTSPARK_TOTP_SECRET", "")
GMAIL_EMAIL     = os.environ.get("GMAIL_EMAIL",            "")
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASSWORD",     "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID   = os.environ.get("SLACK_USER_ID",   "")
S3_ENABLED      = os.environ.get("S3_ENABLED", "false").lower() == "true"
S3_BUCKET       = os.environ.get("S3_BUCKET",  "payout-recon")
S3_PREFIX       = os.environ.get("LIGHTSPARK_S3_PREFIX", "lightspark/raw/")
S3_REGION       = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
DOWNLOAD_DIR        = Path("downloads")
LIGHTSPARK_URL      = "https://app.lightspark.com"
REPORTS_URL         = "https://app.lightspark.com/reports"
LIGHTSPARK_COOKIES  = os.environ.get("LIGHTSPARK_COOKIES", "")
PROXY_HOST     = os.environ.get("PROXY_HOST",     "")
PROXY_PORT     = os.environ.get("PROXY_PORT",     "")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")
IMAP_SERVER         = "imap.gmail.com"
IMAP_PORT           = 993
EMAIL_POLL_INTERVAL = 10
EMAIL_POLL_TIMEOUT  = 300
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

def get_otp() -> str:
    return pyotp.TOTP(LS_TOTP_SECRET).now()

async def ss(page, name: str) -> None:
    path = f"ls_dbg_{name}.png"
    await page.screenshot(path=path)
    print(f"  [screenshot] {path}")

async def _rand_pause(min_ms: int = 300, max_ms: int = 800) -> None:
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)

async def human_type(page, locator, text: str) -> None:
    await locator.click()
    await _rand_pause(100, 300)
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await _rand_pause(80, 200)
    for char in text:
        await page.keyboard.type(char, delay=random.randint(50, 130))
    await _rand_pause(100, 300)

async def apply_stealth(page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 },
                ];
                arr.__proto__ = PluginArray.prototype;
                return arr;
            }
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);
        if (!window.chrome) window.chrome = {};
        window.chrome.runtime = window.chrome.runtime || {};
        Object.defineProperty(navigator, 'userAgent', {
            get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        });
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, parameter);
        };
        const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter2.call(this, parameter);
        };
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });
        Object.defineProperty(navigator, 'platform',            { get: () => 'Win32' });
        Object.defineProperty(navigator, 'vendor',              { get: () => 'Google Inc.' });
        const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type) {
            const result = _origToDataURL.apply(this, arguments);
            return result.slice(0, -4) + (Math.random() < 0.5 ? 'AAAA' : 'BBBB');
        };
        const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function() {
            const imageData = _origGetImageData.apply(this, arguments);
            if (imageData.data.length > 0) imageData.data[0] = imageData.data[0] ^ 1;
            return imageData;
        };
        if (!window.chrome) window.chrome = {};
        window.chrome.csi = function() {
            return { startE: Date.now(), onloadT: Date.now(), pageT: Math.random() * 1000, tran: 15 };
        };
        window.chrome.loadTimes = function() {
            return { requestTime: Date.now()/1000, startLoadTime: Date.now()/1000, commitLoadTime: Date.now()/1000,
                     finishDocumentLoadTime: Date.now()/1000, finishLoadTime: Date.now()/1000, firstPaintTime: Date.now()/1000,
                     firstPaintAfterLoadTime: 0, navigationType: 'Other', wasFetchedViaSpdy: false,
                     wasNpnNegotiated: false, npnNegotiatedProtocol: 'unknown',
                     wasAlternateProtocolAvailable: false, connectionInfo: 'http/1.1' };
        };
        try {
            const _origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function() {
                const data = _origGetChannelData.apply(this, arguments);
                for (let i = 0; i < data.length; i += 100) { data[i] += Math.random() * 0.0001; }
                return data;
            };
        } catch(e) {}
    """)

def notify_slack(message: str, color: str = "good") -> None:
    if not SLACK_BOT_TOKEN or not SLACK_USER_ID:
        print("[slack] Skipping — tokens not set.")
        return
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    channel_id = SLACK_USER_ID
    if SLACK_USER_ID.startswith("U"):
        try:
            r = requests.post("https://slack.com/api/conversations.open",
                              json={"users": SLACK_USER_ID}, headers=headers, timeout=10)
            d = r.json()
            if d.get("ok"):
                channel_id = d["channel"]["id"]
        except Exception as e:
            print(f"[slack] conversations.open error: {e}")
    icon = {"good": ":white_check_mark:", "warning": ":warning:", "danger": ":x:"}.get(color, "")
    payload = {"channel": channel_id, "text": f"{icon} {message}",
               "attachments": [{"color": color, "text": message,
                                 "footer": "Lightspark Exporter", "ts": int(datetime.now().timestamp())}]}
    try:
        resp = requests.post("https://slack.com/api/chat.postMessage",
                             json=payload, headers=headers, timeout=10)
        data = resp.json()
        print("[slack] DM sent." if data.get("ok") else f"[slack] Error: {data.get('error')}")
    except Exception as e:
        print(f"[slack] Failed: {e}")

async def do_login(page) -> None:
    print("[login] Navigating to Lightspark ...")
    await page.goto(LIGHTSPARK_URL, wait_until="domcontentloaded", timeout=30_000)
    await _rand_pause(2_000, 3_000)
    await ss(page, "00_landing")
    await page.mouse.move(random.randint(300, 700), random.randint(200, 400))
    await _rand_pause(300, 600)

    continue_email = page.locator('button:has-text("Continue with email"), a:has-text("Continue with email"), [data-testid*="email"]')
    if await continue_email.count() > 0:
        print("[login] Clicking 'Continue with email' ...")
        await page.mouse.move(random.randint(800, 1000), random.randint(340, 380))
        await _rand_pause(200, 500)
        await continue_email.first.click()
        await _rand_pause(1_500, 2_500)

    email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="email" i]')
    if await email_input.count() > 0:
        print(f"[login] Typing email: {LS_EMAIL}")
        await human_type(page, email_input.first, LS_EMAIL)

    pw_input = page.locator('input[type="password"]')
    if await pw_input.count() == 0:
        next_btn = page.locator('button:has-text("Continue"), button:has-text("Next"), button[type="submit"]')
        if await next_btn.count() > 0:
            print("[login] Clicking Continue to reveal password field ...")
            await _rand_pause(300, 600)
            await next_btn.first.click()
            await _rand_pause(1_500, 2_500)
            pw_input = page.locator('input[type="password"]')

    if await pw_input.count() > 0:
        print("[login] Typing password ...")
        await human_type(page, pw_input.first, LS_PASSWORD)

    await _rand_pause(500, 1_000)

    signin_btn = page.locator('button[type="submit"], button:has-text("Continue with email"), button:has-text("Sign in"), button:has-text("Log in")')
    if await signin_btn.count() > 0:
        print("[login] Clicking Sign in ...")
        await page.mouse.move(random.randint(880, 1050), random.randint(410, 450))
        await _rand_pause(200, 400)
        await signin_btn.first.click()
        await _rand_pause(3_000, 5_000)

    await ss(page, "01_after_signin")

    page_text = await page.inner_text("body")
    if "bot behaviour" in page_text.lower() or "detected bot" in page_text.lower():
        print("[login] WARNING: Bot detected — retrying after 10s ...")
        await _rand_pause(10_000, 12_000)
        try_again = page.locator('button:has-text("try again"), a:has-text("try again")')
        if await try_again.count() > 0:
            await try_again.first.click()
            await _rand_pause(3_000, 5_000)
        else:
            await page.reload(wait_until="domcontentloaded")
            await _rand_pause(3_000, 5_000)
            await do_login_form(page)
            return

    for attempt in range(1, 4):
        digit_inputs     = page.locator('input[maxlength="1"]')
        single_otp_input = page.locator('input[name="otp"], input[autocomplete="one-time-code"], input[placeholder*="code" i], input[placeholder*="authenticator" i], input[type="number"][maxlength="6"], input[maxlength="6"]')
        digit_count  = await digit_inputs.count()
        single_count = await single_otp_input.count()
        if digit_count == 0 and single_count == 0:
            print("[login] No OTP field — proceeding.")
            break
        code = get_otp()
        print(f"[login] OTP attempt {attempt}: {code} (digit_inputs={digit_count}, single={single_count})")
        if digit_count >= 6:
            print("[login] Entering OTP into individual digit boxes ...")
            await digit_inputs.nth(0).click()
            await _rand_pause(200, 400)
            for ch in code:
                await page.keyboard.type(ch)
                await _rand_pause(80, 150)
        else:
            await human_type(page, single_otp_input.first, code)
        await _rand_pause(300, 600)
        otp_submit = page.locator('button[type="submit"], button:has-text("Verify"), button:has-text("Continue"), button:has-text("Confirm")')
        if await otp_submit.count() > 0:
            await otp_submit.first.click()
        await _rand_pause(3_500, 5_000)
        still_digit  = await page.locator('input[maxlength="1"]').count()
        still_single = await single_otp_input.count()
        if still_digit >= 6 or still_single > 0:
            print("[login] OTP not accepted, waiting 15s ...")
            await _rand_pause(15_000, 16_000)
        else:
            break

    try:
        await page.wait_for_url("*app.lightspark.com*", timeout=30_000)
    except PwTimeout:
        pass
    print(f"[login] Current URL: {page.url}")
    await ss(page, "02_logged_in")
    print("[login] Login complete.")

async def do_login_form(page) -> None:
    email_input = page.locator('input[type="email"], input[name="email"]')
    if await email_input.count() > 0:
        await human_type(page, email_input.first, LS_EMAIL)
    pw_input = page.locator('input[type="password"]')
    if await pw_input.count() > 0:
        await human_type(page, pw_input.first, LS_PASSWORD)
    await _rand_pause(500, 1_000)
    signin_btn = page.locator('button[type="submit"], button:has-text("Continue with email")')
    if await signin_btn.count() > 0:
        await signin_btn.first.click()
        await _rand_pause(3_000, 5_000)

async def load_saved_session(context) -> bool:
    if not LIGHTSPARK_COOKIES:
        return False
    try:
        session = json.loads(base64.b64decode(LIGHTSPARK_COOKIES).decode())
        cookies = session.get("cookies", [])
        if cookies:
            await context.add_cookies(cookies)
            print(f"[auth] Loaded {len(cookies)} saved cookies.")
            return True
    except Exception as e:
        print(f"[auth] Failed to load saved cookies: {e}")
    return False

async def ensure_authenticated(page, context) -> None:
    has_cookies = await load_saved_session(context)
    print("[auth] Loading Reports page ...")
    await page.goto(REPORTS_URL, wait_until="networkidle", timeout=60_000)
    print("[auth] Waiting for SPA to render ...")
    page_content_appeared = False
    try:
        await page.wait_for_selector(
            'input[type="email"], button:has-text("Continue with email"), text=Log into Lightspark, text=Generate transaction report, [data-testid*="report"], nav, header',
            timeout=30_000)
        page_content_appeared = True
    except Exception:
        print("[auth] WARNING: Timed out waiting for SPA content")
    await _rand_pause(2_000, 3_000)
    await ss(page, "03_reports_page")
    login_in_url = ("login" in page.url.lower() or "signin" in page.url.lower()
                    or "auth" in page.url.lower() or page.url.rstrip("/") == LIGHTSPARK_URL)
    login_form_visible = (
        await page.locator('button:has-text("Continue with email")').count() > 0
        or await page.locator('text=Log into Lightspark').count() > 0
        or await page.locator('input[type="email"]').count() > 0)
    force_login   = not page_content_appeared and not has_cookies
    is_login_page = login_in_url or login_form_visible or force_login
    if is_login_page:
        if has_cookies and not force_login:
            raise RuntimeError("Saved session cookies have expired. Re-run helper_save_lightspark_session.py.")
        print("[auth] Falling back to login form ...")
        await do_login(page)
        await page.goto(REPORTS_URL, wait_until="networkidle", timeout=60_000)
        try:
            await page.wait_for_selector('text=Generate transaction report, nav, header', timeout=30_000)
        except Exception:
            pass
        await _rand_pause(3_000, 4_000)
        await ss(page, "03b_reports_page_after_login")
    else:
        print("[auth] Session valid — already on Reports page.")

async def generate_csv_report(page) -> None:
    print("[report] Clicking 'Generate transaction report' ...")
    gen_btn = page.locator('button:has-text("Generate transaction report"), a:has-text("Generate transaction report"), span:has-text("Generate transaction report"), [data-testid*="generate-transaction"]')
    if await gen_btn.count() == 0:
        gen_btn = page.locator('text=Generate transaction report')
    await gen_btn.first.click(timeout=20_000)
    await _rand_pause(2_000, 3_000)
    await ss(page, "04_report_dialog")
    print("[report] Selecting 'Custom' date range ...")
    custom_btn = page.locator('button:has-text("Custom"), [data-testid*="custom"]')
    if await custom_btn.count() > 0:
        await custom_btn.first.click()
        await _rand_pause(800, 1_200)
    await ss(page, "05_custom_selected")
    start_str = to_lightspark_date(START_DT)
    end_str   = to_lightspark_date(END_DT)
    print(f"[report] Setting dates: {start_str} -> {end_str}")
    date_inputs = page.locator('dialog input[type="text"], [role="dialog"] input[type="text"], .modal input[type="text"]')
    if await date_inputs.count() < 2:
        date_inputs = page.locator('input[type="text"]')
    count = await date_inputs.count()
    print(f"[report] Found {count} text input(s)")
    if count >= 1:
        await date_inputs.nth(0).triple_click()
        await date_inputs.nth(0).fill(start_str)
        await page.keyboard.press("Tab")
        await _rand_pause(300, 500)
    if count >= 2:
        await date_inputs.nth(1).triple_click()
        await date_inputs.nth(1).fill(end_str)
        await page.keyboard.press("Tab")
        await _rand_pause(300, 500)
    await ss(page, "06_dates_filled")
    print("[report] Ensuring all transaction types are checked ...")
    checkboxes = page.locator('input[type="checkbox"]')
    for i in range(await checkboxes.count()):
        if not await checkboxes.nth(i).is_checked():
            await checkboxes.nth(i).click()
            await _rand_pause(100, 200)
    await _rand_pause(500, 800)
    print("[report] Clicking 'Generate CSV' ...")
    gen_csv_btn = page.locator('button:has-text("Generate CSV"), [data-testid*="generate-csv"]')
    if await gen_csv_btn.count() == 0:
        gen_csv_btn = page.locator('button').filter(has_text="Generate CSV")
    await gen_csv_btn.first.click(timeout=10_000)
    await _rand_pause(2_000, 3_000)
    await ss(page, "07_csv_requested")
    print("[report] CSV generation requested. Lightspark will email the download link.")

def _decode_str(s) -> str:
    parts = decode_header(s)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)

def _extract_urls_from_email(msg) -> list[str]:
    urls = []
    url_pattern = re.compile(r'https?://[^\s\'"<>]+lightspark[^\s\'"<>]*', re.IGNORECASE)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/html", "text/plain"):
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    urls.extend(url_pattern.findall(body))
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            urls.extend(url_pattern.findall(body))
        except Exception:
            pass
    cleaned = [u.replace("&amp;", "&").rstrip(".,;)") for u in urls]
    return list(dict.fromkeys(cleaned))

def poll_gmail_for_download_link(triggered_after: float) -> str:
    if not GMAIL_EMAIL or not GMAIL_APP_PASS:
        raise RuntimeError("GMAIL_EMAIL or GMAIL_APP_PASSWORD not set.")
    print(f"[gmail] Connecting to {IMAP_SERVER} ...")
    deadline = time.time() + EMAIL_POLL_TIMEOUT
    poll_num = 0
    while time.time() < deadline:
        poll_num += 1
        try:
            with imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) as imap:
                imap.login(GMAIL_EMAIL, GMAIL_APP_PASS)
                imap.select("INBOX")
                since_date = datetime.fromtimestamp(triggered_after).strftime("%d-%b-%Y")
                status, msg_ids = imap.search(None, f'(SINCE "{since_date}" FROM "lightspark")')
                if status != "OK":
                    time.sleep(EMAIL_POLL_INTERVAL)
                    continue
                ids = msg_ids[0].split()
                print(f"[gmail] Poll {poll_num}: found {len(ids)} Lightspark email(s)")
                for msg_id in reversed(ids):
                    status, data = imap.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        continue
                    msg  = email_lib.message_from_bytes(data[0][1])
                    subj = _decode_str(msg.get("Subject", ""))
                    print(f"[gmail]   Subject: {subj}")
                    urls = _extract_urls_from_email(msg)
                    print(f"[gmail]   URLs found: {len(urls)}")
                    for url in urls:
                        print(f"[gmail]     {url}")
                    for kw in ["download", "report", "export", "csv", "transaction"]:
                        for url in urls:
                            if kw in url.lower():
                                print(f"[gmail] Download URL: {url}")
                                return url
                    if urls:
                        return urls[0]
        except imaplib.IMAP4.error as e:
            print(f"[gmail] IMAP error: {e}")
        except Exception as e:
            print(f"[gmail] Unexpected error: {e}")
        remaining = int(deadline - time.time())
        print(f"[gmail] Waiting {EMAIL_POLL_INTERVAL}s ({remaining}s remaining) ...")
        time.sleep(EMAIL_POLL_INTERVAL)
    raise RuntimeError(f"Lightspark download email not received within {EMAIL_POLL_TIMEOUT}s.")

async def download_via_email_link(page, download_url: str) -> Path:
    print("[download] Navigating to download URL ...")
    await page.goto(download_url, wait_until="domcontentloaded", timeout=30_000)
    await _rand_pause(3_000, 4_000)
    await ss(page, "08_download_page")
    if any(x in page.url.lower() for x in ("login", "signin", "auth")):
        print("[download] Session expired — re-logging in ...")
        await do_login(page)
        await page.goto(download_url, wait_until="domcontentloaded", timeout=30_000)
        await _rand_pause(3_000, 4_000)
        await ss(page, "08b_download_page_after_login")
    print("[download] Looking for Download button ...")
    download_btn = page.locator('button:has-text("Download"), a:has-text("Download"), [data-testid*="download"], button:has-text("Download CSV"), a[download], a[href*="download"], a[href*=".csv"]')
    if await download_btn.count() == 0:
        download_btn = page.locator('button').filter(has_text=re.compile(r'download', re.I))
    print(f"[download] Found {await download_btn.count()} download button(s)")
    await ss(page, "09_before_download")
    async with page.expect_download(timeout=60_000) as dl_info:
        await download_btn.first.click(timeout=15_000)
    dl   = await dl_info.value
    dest = DOWNLOAD_DIR / EXPORT_FILENAME
    await dl.save_as(dest)
    print(f"[download] Saved -> {dest.resolve()}")
    await ss(page, "10_downloaded")
    return dest

def upload_to_s3(local_path: Path) -> str:
    s3_key = f"{S3_PREFIX}{local_path.name}"
    print(f"[s3] Uploading to s3://{S3_BUCKET}/{s3_key} ...")
    try:
        s3 = boto3.client("s3", region_name=S3_REGION,
                          aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                          aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"))
        s3.upload_file(str(local_path), S3_BUCKET, s3_key, ExtraArgs={"ContentType": "text/csv"})
        uri = f"s3://{S3_BUCKET}/{s3_key}"
        print(f"[s3] Upload complete -> {uri}")
        return uri
    except NoCredentialsError:
        print("[s3] ERROR: AWS credentials not found")
        raise
    except ClientError as e:
        print(f"[s3] ERROR: {e.response['Error']['Code']} - {e.response['Error']['Message']}")
        raise

async def main() -> None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    print(f"[*] Date range : {_start_raw}  ->  {_end_raw}")
    print(f"[*] Export file: {EXPORT_FILENAME}")
    print(f"[*] S3 prefix  : {S3_PREFIX}")
    IS_CI   = os.environ.get("CI", "false").lower() == "true"
    SLOW_MO = 50 if IS_CI else 100
    print(f"[*] Mode       : {'CI/headless (xvfb)' if IS_CI else 'local/headed'}")
    csv_path = None
    s3_uri   = None
    proxy_config = None
    if PROXY_HOST and PROXY_PORT:
        proxy_url    = f"http://{PROXY_HOST}:{PROXY_PORT}"
        proxy_config = {"server": proxy_url}
        if PROXY_USERNAME and PROXY_PASSWORD:
            proxy_config["username"] = PROXY_USERNAME
            proxy_config["password"] = PROXY_PASSWORD
        print(f"[*] Proxy      : {proxy_url}")
    else:
        print("[*] Proxy      : none")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False, slow_mo=SLOW_MO, proxy=proxy_config,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-infobars", "--disable-extensions",
                  "--no-first-run", "--no-default-browser-check", "--window-size=1440,900"])
        context = await browser.new_context(
            accept_downloads=True, viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT, locale="en-US", timezone_id="America/New_York",
            proxy=proxy_config)
        page = await context.new_page()
        await apply_stealth(page)
        try:
            await ensure_authenticated(page, context)
            triggered_at = time.time()
            await generate_csv_report(page)
            print(f"\n[*] Polling Gmail for download link (up to {EMAIL_POLL_TIMEOUT}s) ...")
            download_url = poll_gmail_for_download_link(triggered_at)
            csv_path = await download_via_email_link(page, download_url)
        except Exception as exc:
            msg = f"Lightspark export FAILED\nPeriod : {_start_raw} -> {_end_raw}\nError  : {exc}"
            print(f"\n[!] {msg}")
            notify_slack(msg, color="danger")
            try:
                await page.screenshot(path="ls_error_final.png")
            except Exception:
                pass
            raise
        finally:
            await browser.close()
    if csv_path and S3_ENABLED:
        try:
            s3_uri = upload_to_s3(csv_path)
        except Exception as e:
            notify_slack(f"Lightspark export: S3 upload FAILED\nError: {e}", color="warning")
    if csv_path:
        size_kb = csv_path.stat().st_size // 1024
        lines = ["*Lightspark Export Complete*",
                 f"Period : `{_start_raw}`  ->  `{_end_raw}`",
                 f"File   : `{csv_path.name}` ({size_kb} KB)"]
        if s3_uri:
            lines.append(f"S3     : `{s3_uri}`")
        notify_slack("\n".join(lines), color="good")
        print(f"\n[+] All done!")
        print(f"    File : {csv_path.resolve()}")
        if s3_uri:
            print(f"    S3   : {s3_uri}")

if __name__ == "__main__":
    asyncio.run(main())
