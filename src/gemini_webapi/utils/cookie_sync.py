import os
import sys
import argparse
import asyncio
import logging
from playwright.async_api import async_playwright
from .profile_utils import resolve_paths, save_cookies_to_json


logger = logging.getLogger("sync_cookies")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)


async def sync_account(account: str, headless: bool = True, profiles_dir: str = "profiles") -> bool:
    """
    Main asynchronous function to sync account cookies.
    Can be called programmatically by the proxy or via CLI.
    """
    account_id, cookie_path, profile_path = resolve_paths(account, profiles_dir)

    logger.info(f"Starting sync for account: {account_id}")
    logger.info(f"Profile folder: {profile_path}")
    logger.info(f"Saving cookies to: {cookie_path}")

    # Parameters to disguise automation (anti-bot)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox"
    ]

    success = False

    try:
        async with async_playwright() as p:
            # Launch browser using the persistent profile directory
            context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=headless,
                channel="chrome" if os.name == "nt" else None,  # Use installed Chrome if available
                args=launch_args,
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            page = await context.new_page()

            # Add simple script to mitigate automation detection
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            cookies = []
            if not headless:
                logger.info("Visual Mode Started. Please log in manually if needed.")
                await page.goto("https://gemini.google.com/app")

                print("\n=======================================================")
                print(f"Window opened for account '{account_id}'.")
                print("1. Log in to your Google account, if necessary.")
                print("2. Make sure the Gemini Web interface has loaded.")
                print("3. WHEN LOGIN IS COMPLETE, JUST CLOSE THE BROWSER WINDOW")
                print("   to save cookies and finish.")
                print("=======================================================\n")

                # Monitor the window in real-time and capture cookies continuously
                last_valid_cookies = []
                try:
                    last_valid_cookies = await context.cookies()
                except Exception:
                    pass

                try:
                    while True:
                        if page.is_closed() or len(context.pages) == 0:
                            break

                        # Periodically extract cookies while the browser is open
                        current_cookies = await context.cookies()
                        psid = None
                        psidts = None
                        for cookie in current_cookies:
                            if cookie["name"] == "__Secure-1PSID":
                                psid = cookie["value"]
                            elif cookie["name"] == "__Secure-1PSIDTS":
                                psidts = cookie["value"]

                        if psid and psidts:
                            last_valid_cookies = current_cookies

                        await asyncio.sleep(0.5)
                except Exception:
                    pass

                cookies = last_valid_cookies
            else:
                logger.info("Accessing Gemini in the background (headless)...")
                await page.goto("https://gemini.google.com/app", timeout=45000)
                # Wait for initial load and cookies to rotate
                await page.wait_for_timeout(10000)

                # Check if redirected to login page
                current_url = page.url
                if "accounts.google.com" in current_url or "/app" not in current_url:
                    logger.error(f"Session for '{account_id}' has expired or is not logged in! Manual login is required using the --login parameter.")
                    await context.close()
                    return False

                # Extract cookies from context
                cookies = await context.cookies()

            psid = None
            psidts = None
            for cookie in cookies:
                if cookie["name"] == "__Secure-1PSID":
                    psid = cookie["value"]
                elif cookie["name"] == "__Secure-1PSIDTS":
                    psidts = cookie["value"]

            if psid and psidts:
                # Validate if extracted cookies are actually valid and logged in
                from gemini_webapi import GeminiClient
                from gemini_webapi.constants import AccountStatus

                logger.info(f"Validating extracted cookies for '{account_id}'...")
                test_client = GeminiClient(psid, psidts)
                is_valid = False
                try:
                    await test_client.init(timeout=15)
                    is_valid = (test_client.account_status != AccountStatus.UNAUTHENTICATED)
                except Exception as e:
                    logger.warning(f"Error validating extracted cookies for '{account_id}': {e}")
                finally:
                    try:
                        await test_client.close()
                    except Exception:
                        pass

                if is_valid:
                    save_cookies_to_json(cookie_path, psid, psidts)
                    logger.info(f"Cookies updated successfully for '{account_id}'!")
                    success = True
                else:
                    logger.error(f"Extracted cookies for '{account_id}' are invalid or expired. Not saving.")
                    success = False
            else:
                logger.warning(f"Cookies __Secure-1PSID or __Secure-1PSIDTS were not found for '{account_id}'.")

            try:
                await context.close()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error during sync for account '{account_id}': {e}", exc_info=True)

    return success


def main_cli():
    # Set Windows Event Loop Policy if on win32
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="Cookie Synchronizer for Gemini Web")
    parser.add_argument("account", nargs="?", default=None, help="Account name or cookie path (e.g. cookies, cookies2, cookies3)")
    parser.add_argument("--login", action="store_true", help="Open visible browser for manual login")
    parser.add_argument("--refresh", action="store_true", help="Run silent update (headless)")
    parser.add_argument("--verify-all", action="store_true", help="Open all windows visually to verify login")

    args = parser.parse_args()

    if args.verify_all:
        from .profile_utils import get_all_profiles
        print("\n=======================================================")
        print("          VERIFICATION OF GEMINI PROXY LOGINS          ")
        print("=======================================================")

        check_existing = input("\nDo you want to verify logins of existing accounts? (y/N): ").strip().lower()
        if check_existing in ('y', 'yes'):
            accounts = get_all_profiles()
            print(f"\n[INFO] Starting visual verification for {len(accounts)} accounts: {', '.join(accounts)}")
            for idx, acc in enumerate(accounts, start=1):
                print(f"\n=======================================================")
                print(f" Verifying Account {idx}/{len(accounts)}: {acc}")
                print(f"=======================================================")
                # Run visually
                success = asyncio.run(sync_account(acc, headless=False))
                if success:
                    print(f"[SUCCESS] Account '{acc}' successfully verified/logged in!")
                else:
                    print(f"[WARNING] Failed to verify/log in account '{acc}'.")
        else:
            print("\n[INFO] Skipping verification of existing accounts.")

        print("\n-------------------------------------------------------")
        while True:
            add_new = input("Do you want to add a new account (open a new window)? (y/N): ").strip().lower()
            if add_new in ('y', 'yes'):
                new_name = input("Enter the name/identifier for the new account (e.g. account4, cookies4): ").strip()
                if not new_name:
                    print("[WARNING] Invalid name. Please try again.")
                    continue
                new_name = new_name.replace(" ", "_").replace(".json", "")
                print(f"\nOpening window for new account '{new_name}'...")
                success = asyncio.run(sync_account(new_name, headless=False))
                if success:
                    print(f"[SUCCESS] New account '{new_name}' configured!")
                else:
                    print(f"[WARNING] Failed to configure new account '{new_name}'.")
            else:
                break
        print("\n=======================================================")
        print("    CONFIGURATION FINISHED. THE PROXY SERVER WILL START")
        print("=======================================================\n")
    else:
        if not args.account:
            from .profile_utils import get_active_profile
            active_prof = get_active_profile()
            args.account = active_prof or "cookies"
        headless_mode = not args.login
        asyncio.run(sync_account(args.account, headless=headless_mode))

