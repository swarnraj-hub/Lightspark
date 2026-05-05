import asyncio
import base64
from playwright.async_api import async_playwright


async def main():
    print("Opening Lightspark in a real browser ...")
    print("Log in normally (email + password + OTP).")
    print("Once dashboard loads, press ENTER.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=50
        )

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}
        )

        page = await context.new_page()
        await page.goto("https://app.lightspark.com", wait_until="domcontentloaded")

        # Wait for manual login
        input("\n>>> Press ENTER after login is complete...\n")

        print(f"[info] Current URL: {page.url}")

        # 🔥 CORRECT WAY
        await context.storage_state(path="session.json")

        # encode for GitHub
        with open("session.json", "rb") as f:
            encoded = base64.b64encode(f.read()).decode()

        with open("session.b64", "w") as f:
            f.write(encoded)

        print("\n[+] Session saved:")
        print("    - session.json (local use)")
        print("    - session.b64 (for GitHub secret)")

        print("\nNext steps:")
        print("1. Copy session.b64")
        print("2. Add GitHub Secret: LIGHTSPARK_SESSION")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
