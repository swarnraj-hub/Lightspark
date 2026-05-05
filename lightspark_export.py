#!/usr/bin/env python3

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
from playwright.async_api import async_playwright

# =========================
# CONFIG
# =========================

LIGHTSPARK_URL = "https://app.lightspark.com"
REPORTS_URL = "https://app.lightspark.com/reports"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")

S3_BUCKET = os.getenv("S3_BUCKET", "payout-recon")
S3_PREFIX = os.getenv("LIGHTSPARK_S3_PREFIX", "lightspark/raw/")

# =========================
# SESSION LOADER
# =========================

def load_session_from_env():
    data = os.getenv("LIGHTSPARK_SESSION")
    if not data:
        return None
    try:
        decoded = base64.b64decode(data).decode()
        return json.loads(decoded)
    except Exception as e:
        print("[auth] Failed to decode session:", e)
        return None

# =========================
# AUTH (NO LOGIN HERE)
# =========================

async def ensure_auth(page):
    await page.goto(REPORTS_URL, wait_until="domcontentloaded")

    if "login" in page.url.lower():
        raise Exception("❌ Session expired. Recreate session.json locally.")

    print("[auth] Session valid")

# =========================
# REPORT
# =========================

async def generate_report(page, start, end):
    await page.goto(REPORTS_URL)

    btn = page.locator("text=Generate transaction report")
    await btn.first.click()

    await asyncio.sleep(3)

    inputs = page.locator("input[type='text']")
    await inputs.nth(0).fill(start)
    await page.keyboard.press("Tab")
    await inputs.nth(1).fill(end)

    gen = page.locator("button:has-text('Generate CSV')")
    await gen.first.click()

    print("[report] CSV requested")

# =========================
# EMAIL
# =========================

def extract_links(msg):
    return re.findall(r'https?://[^\s"]+', msg)

def poll_email():
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_EMAIL, GMAIL_PASS)
    imap.select("inbox")

    for _ in range(30):
        _, msgs = imap.search(None, 'FROM "lightspark"')
        ids = msgs[0].split()

        for i in reversed(ids):
            _, data = imap.fetch(i, "(RFC822)")
            msg = email_lib.message_from_bytes(data[0][1])

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        body += part.get_payload(decode=True).decode()
            else:
                body = msg.get_payload(decode=True).decode()

            urls = extract_links(body)
            if urls:
                print("[gmail] found link")
                return urls[0]

        time.sleep(10)

    raise Exception("No email received")

# =========================
# DOWNLOAD
# =========================

async def download_file(page, url, filename):
    await page.goto(url)

    btn = page.locator("text=Download")

    async with page.expect_download() as d:
        await btn.first.click()

    download = await d.value
    path = DOWNLOAD_DIR / filename
    await download.save_as(path)

    print(f"[download] saved {path}")
    return path

# =========================
# S3
# =========================

def upload_s3(path):
    s3 = boto3.client("s3")
    key = f"{S3_PREFIX}{path.name}"
    s3.upload_file(str(path), S3_BUCKET, key)
    uri = f"s3://{S3_BUCKET}/{key}"
    print("[s3]", uri)
    return uri

# =========================
# MAIN
# =========================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start_date")
    parser.add_argument("--end_date")
    args = parser.parse_args()

    filename = f"LIGHTSPARK_{args.start_date}_to_{args.end_date}.csv"

    session_data = load_session_from_env()

    if not session_data:
        raise Exception("❌ LIGHTSPARK_SESSION not set")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        context = await browser.new_context(
            storage_state=session_data,
            viewport={"width": 1440, "height": 900}
        )

        page = await context.new_page()

        # ✅ NO LOGIN
        await ensure_auth(page)

        # ✅ REPORT
        await generate_report(page, args.start_date, args.end_date)

        print("[*] waiting for email...")
        url = poll_email()

        print("[*] downloading...")
        path = await download_file(page, url, filename)

        print("[*] uploading to S3...")
        upload_s3(path)

        print("[✓] DONE")

        await browser.close()

# =========================

if __name__ == "__main__":
    asyncio.run(main())
