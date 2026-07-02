import os
import asyncio
from gemini_webapi.constants import AccountStatus
from .profile_utils import get_all_profiles, load_cookies_from_json, resolve_paths


async def check_profile(profile_name: str, profiles_dir: str = "profiles") -> dict:
    from gemini_webapi import GeminiClient
    account_id, cookie_path, profile_path = resolve_paths(profile_name, profiles_dir)

    result = {
        "profile": profile_name,
        "cookie_file": "Missing",
        "session": "Unauthenticated",
        "details": "No cookies.json file found. Please log in using select_model.py"
    }

    if os.path.exists(cookie_path):
        result["cookie_file"] = "Found"
        psid, psidts = load_cookies_from_json(cookie_path)
        if not psid or not psidts:
            result["details"] = "cookies.json exists but is malformed or missing key fields."
            return result

        client = GeminiClient(psid, psidts)
        try:
            await client.init(timeout=10)
            if client.account_status == AccountStatus.AVAILABLE:
                result["session"] = "Logged In"
                result["details"] = f"Successfully authenticated. Available models: {len(client.list_models())}"
            else:
                result["details"] = f"Google returned status: {client.account_status.name} ({client.account_status.description})"
        except Exception as e:
            result["details"] = f"Verification request failed: {e}"
        finally:
            try:
                await client.close()
            except Exception:
                pass
    return result


async def run_health_check(profiles_dir: str = "profiles"):
    print("\n======================================================================")
    print("                 GEMINI PROXY PROFILE HEALTH CHECK                    ")
    print("======================================================================")

    profiles = get_all_profiles(profiles_dir)
    print(f"Detected {len(profiles)} profiles in the '{profiles_dir}/' directory.")
    print("Checking connectivity to Gemini API for each profile...\n")

    tasks = [check_profile(p, profiles_dir) for p in profiles]
    results = await asyncio.gather(*tasks)

    # Print clean markdown-style table
    print(f"{'PROFILE':<15} | {'COOKIES.JSON':<12} | {'STATUS':<15} | {'DETAILS'}")
    print("-" * 80)
    for r in results:
        status_str = r["session"]
        if status_str == "Logged In":
            status_formatted = f"[OK] {status_str}"
        else:
            status_formatted = f"[FAIL] {status_str}"

        print(f"{r['profile']:<15} | {r['cookie_file']:<12} | {status_formatted:<15} | {r['details']}")

    print("======================================================================\n")
