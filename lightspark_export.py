#!/usr/bin/env python3

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
from datetime import datetime
from pathlib import Path
from email.header import decode_header
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# =========================
# CONFIG
# =========================

LIGHTSPARK_URL = "https://app.lightspark.com"
REPORTS_URL = "https://app.lightspark.com/reports"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ENV
LS_EMAIL = os.getenv("LIGHTSPARK_EMAIL")
LS_PASSWORD = os.getenv("LIGHTSPARK_PASSWORD")
LS_TOTP_SECRET = os.getenv("LIGHTSPARK_TOTP_SECRET")

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")

S3_BUCKET = os.getenv("S3_BUCKET", "payout-recon")
S3_PREFIX = os.getenv("LIGHTSPARK_S3_PREFIX", "lightspark/raw/")

# =========================
# UTILS
# =========================

def get_otp():
    return pyotp.TOTP(LS_TOTP_SECRET).now()

async def rand_sleep(a=500, b=1200):
    await asyncio.sleep(random.randint(a, b) / 1000)

async def human_type(page, locator, text):
    await locator.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    for c in text:
        await page.keyboard.type(c, delay=random.randint(60, 120))

async def wait_and_click_enabled(page, locator):
    btn = locator.first
    await btn.wait_for(state="visible", timeout=20000)

    # 🔥 critical fix
    await page.wait_for_function("(el) => !el.disabled", btn)

    await rand_sleep(800, 1500)
    await btn.click()

# =========================
# LOGIN
# =========================

async def do_login(page):
    print("[login] opening...")
    await page.goto(LIGHTSPARK_URL, wait_until="domcontentloaded")

    await rand_sleep(2000, 3000)

    btn = page.locator("button:has-text('Continue with email')")
    if await btn.count():
        await btn.first.click()

    await rand_sleep(1500, 2500)

    email = page.locator("input[type='email']")
    await human_type(page, email, LS_EMAIL)

    password = page.locator("input[type='password']")
    await human_type(page, password, LS_PASSWORD)

    # 🔥 trigger validation
    await page.keyboard.press("Tab")

    await rand_sleep()

    signin = page.locator("button[type='submit']")
    print("[login] waiting button enable...")
    await wait_and_click_enabled(page, signin)

    await rand_sleep(4000, 6000)

    # BOT detection
    body = (await page.content()).lower()
    if "bot" in body or "suspicious" in body:
        print("[login] bot detected → retrying")
        await asyncio.sleep(15)
        await page.reload()
        return await do_login(page)

    # OTP
    otp_input = page.locator("input[autocomplete='one-time-code'], input[maxlength='6']")
    if await otp_input.count():
        code = get_otp()
        print("[login] entering otp:", code)
        await human_type(page, otp_input.first, code)

        submit = page.locator("button[type='submit']")
        await wait_and_click_enabled(page, submit)

    print("[login] success")

# =========================
# AUTH
# =========================

async def ensure_auth(page, context):
    await page.goto(REPORTS_URL)

    if "login" in page.url.lower():
        print("[auth] need login")
        await do_login(page)
        await context.storage_state(path="session.json")
    else:
        print("[auth] session valid")

# =========================
# REPORT
# =========================

async def generate_report(page, start, end):
    await page.goto(REPORTS_URL)

    btn = page.locator("text=Generate transaction report")
    await btn.first.click()

    await rand_sleep(2000, 3000)

    await page.fill("input[type='text']", start)
    await page.keyboard.press("Tab")
    await page.fill("input[type='text']", end)

    gen = page.locator("button:has-text('Generate CSV')")
    await wait_and_click_enabled(page, gen)

# =========================
# GMAIL
# =========================

def extract_links(msg):
    urls = re.findall(r'https?://[^\s"]+', msg)
    return urls

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
                return urls[0]

        time.sleep(10)

    raise Exception("No email")

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

    return path

# =========================
# S3
# =========================

def upload_s3(path):
    s3 = boto3.client("s3")
    key = f"{S3_PREFIX}{path.name}"
    s3.upload_file(str(path), S3_BUCKET, key)
    return f"s3://{S3_BUCKET}/{key}"

# =========================
# MAIN
# =========================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start_date")
    parser.add_argument("--end_date")
    args = parser.parse_args()

    start = args.start_date
    end = args.end_date

    filename = f"LIGHTSPARK_{start}_to_{end}.csv"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )

        if Path("session.json").exists():
            context = await browser.new_context(storage_state="session.json")
        else:
            context = await browser.new_context()

        page = await context.new_page()

        await ensure_auth(page, context)

        await generate_report(page, start, end)

        print("[*] waiting email...")
        url = poll_email()

        print("[*] downloading...")
        path = await download_file(page, url, filename)

        print("[*] uploading s3...")
        s3_url = upload_s3(path)

        print("[✓] done:", s3_url)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
