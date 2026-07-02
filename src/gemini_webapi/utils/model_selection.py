import os
import sys
import asyncio
from .profile_utils import (
    get_all_profiles,
    resolve_paths,
    load_cookies_from_json,
    SELECTED_PROFILE_FILE,
    SELECTED_MODEL_FILE,
    SELECTED_THINKING_FILE,
)


async def run_model_selection():
    from gemini_webapi import GeminiClient
    profiles = get_all_profiles()

    selected_profile_file = SELECTED_PROFILE_FILE
    current_profile = ""
    if os.path.exists(selected_profile_file):
        try:
            with open(selected_profile_file, "r", encoding="utf-8") as f:
                current_profile = f.read().strip()
        except Exception:
            pass

    default_idx = 1
    if current_profile and current_profile in profiles:
        default_idx = profiles.index(current_profile) + 1

    print("\n======================================================================")
    print("             SELECT BROWSER PROFILE FOR SETUP                         ")
    print("======================================================================")
    for idx, prof in enumerate(profiles, start=1):
        print(f" {idx}. {prof}")
    print("----------------------------------------------------------------------")

    selected_profile = profiles[default_idx - 1]
    try:
        prof_choice = input(f"Choose a profile (1-{len(profiles)}) [Default: {default_idx}]: ")
        if prof_choice.strip():
            selected_idx = int(prof_choice) - 1
            if 0 <= selected_idx < len(profiles):
                selected_profile = profiles[selected_idx]
            else:
                print("Invalid option. Using default profile.")
    except Exception:
        print("Invalid option. Using default profile.")

    print(f"\nUsing profile: '{selected_profile}'")

    # Resolve cookies for the selected profile
    from .cookie_sync import sync_account
    account_id, cookie_path, profile_path = resolve_paths(selected_profile)

    # Try to load existing cookies
    psid, psidts = None, None
    if os.path.exists(cookie_path):
        psid, psidts = load_cookies_from_json(cookie_path)

    # Check if we can initialize GeminiClient
    client = None
    is_authenticated = False

    if psid and psidts:
        print("\nConnecting to Gemini to verify existing session...")
        client = GeminiClient(psid, psidts)
        try:
            await client.init(timeout=15)
            from gemini_webapi.constants import AccountStatus
            if client.account_status != AccountStatus.UNAUTHENTICATED:
                is_authenticated = True
            else:
                print("Existing session is expired or unauthenticated.")
        except Exception as e:
            print(f"Could not connect with existing cookies: {e}")

    if not is_authenticated:
        print(f"\n[INFO] Profile '{selected_profile}' is not logged in or session has expired.")
        login_choice = input(f"Do you want to open a browser window to log in now? (y/N): ").strip().lower()
        if login_choice in ('y', 'yes'):
            print(f"\nOpening browser window for profile '{selected_profile}'...")
            print("Please log in to your Google Account in the opened window, then close the browser to finish.")
            success = await sync_account(selected_profile, headless=False)
            if success and os.path.exists(cookie_path):
                psid, psidts = load_cookies_from_json(cookie_path)
                if psid and psidts:
                    client = GeminiClient(psid, psidts)
                    try:
                        await client.init(timeout=30)
                        from gemini_webapi.constants import AccountStatus
                        if client.account_status != AccountStatus.UNAUTHENTICATED:
                            is_authenticated = True
                        else:
                            print("\n[ERROR] Login was completed but session is still unauthenticated.")
                    except Exception as e:
                        print(f"\n[ERROR] Failed to verify login session: {e}")
            else:
                print("\n[ERROR] Login process failed or was canceled.")
        else:
            print("\n[ERROR] Authentication is required to proceed.")
            sys.exit(1)

    if not is_authenticated or not client:
        print("\n[ERROR] Could not authenticate with Gemini. Exiting.")
        sys.exit(1)

    print(f"\n[SUCCESS] Connected to Gemini using profile '{selected_profile}'!")

    try:
        models = client.list_models()
    except Exception as e:
        print(f"\n[WARNING] Failed to list models from Gemini API: {e}")
        models = []

    if not models:
        print("\n[INFO] Using offline predefined model fallback registry.")
        class FallbackModel:
            def __init__(self, model_id, display_name, description):
                self.model_id = model_id
                self.display_name = display_name
                self.description = description

        models = [
            FallbackModel("gemini-3-flash", "Gemini 3.5 Flash", "All-around help / Fast responses"),
            FallbackModel("gemini-3-flash-thinking", "Gemini 3.5 Flash Thinking", "Solves complex problems / Deep reasoning"),
            FallbackModel("gemini-3-pro", "Gemini 3 Pro", "Standard pro tier model")
        ]

    # Sort the models consistently
    models = sorted(models, key=lambda m: m.display_name)

    print("\n======================================================================")
    print("             AVAILABLE MODELS IN YOUR GEMINI ACCOUNT                  ")
    print("======================================================================")
    for idx, model in enumerate(models, start=1):
        print(f" {idx}. {model.display_name} ({model.model_id})")
        if model.description:
            print(f"    Description: {model.description}")
        print("----------------------------------------------------------------------")

    selected_model_file = SELECTED_MODEL_FILE
    current_selection = ""
    if os.path.exists(selected_model_file):
        try:
            with open(selected_model_file, "r", encoding="utf-8") as f:
                current_selection = f.read().strip()
        except Exception:
            pass

    # Try to set the default option based on the previous selection
    default_index = 1
    if current_selection:
        for idx, model in enumerate(models, start=1):
            if model.model_id == current_selection:
                default_index = idx
                break

    try:
        choice = input(f"Choose a model (1-{len(models)}) [Default: {default_index}]: ")
        if not choice.strip():
            selected_idx = default_index - 1
        else:
            selected_idx = int(choice) - 1
            if selected_idx < 0 or selected_idx >= len(models):
                raise ValueError()
    except (ValueError, IndexError):
        print(f"\nInvalid option. Using the default option.")
        selected_idx = default_index - 1

    selected_model = models[selected_idx]
    print(f"\n[SUCCESS] Selected model: {selected_model.display_name} ({selected_model.model_id})")

    # Choose Thinking Level
    print("\n======================================================================")
    print("             CHOOSE THINKING LEVEL                                    ")
    print("======================================================================")
    print(" 1. Standard (Best for most questions / Fast responses / No CoT)")
    print(" 2. Extended (Complex problem solving / Deep thinking / With CoT)")
    print("----------------------------------------------------------------------")

    selected_thinking_file = SELECTED_THINKING_FILE
    current_thinking = "Standard"
    if os.path.exists(selected_thinking_file):
        try:
            with open(selected_thinking_file, "r", encoding="utf-8") as f:
                current_thinking = f.read().strip()
        except Exception:
            pass

    default_thinking_idx = 1
    if current_thinking == "Extended":
        default_thinking_idx = 2

    # If the user selected a thinking model, default the thinking level choice to Extended (2)
    if "thinking" in selected_model.model_id.lower() or "thinking" in selected_model.display_name.lower():
        default_thinking_idx = 2

    try:
        think_choice = input(f"Choose Thinking Level (1-2) [Default: {default_thinking_idx}]: ")
        if not think_choice.strip():
            selected_think_idx = default_thinking_idx - 1
        else:
            selected_think_idx = int(think_choice) - 1
            if selected_think_idx not in (0, 1):
                raise ValueError()
    except ValueError:
        print("\nInvalid option. Using the default option.")
        selected_think_idx = default_thinking_idx - 1

    selected_thinking = "Standard" if selected_think_idx == 0 else "Extended"
    print(f"\n[SUCCESS] Thinking Level configured to: {selected_thinking}\n")

    # Choose Headless Mode
    print("\n======================================================================")
    print("             CHOOSE BROWSER VISIBILITY                                ")
    print("======================================================================")
    print(" Do you want the browser window to be visible during execution?")
    print(" [Standard: background mode (N) / Visible: headed mode (y)]")
    print("----------------------------------------------------------------------")

    selected_headless_file = ".headless"
    current_headless = "True"
    if os.path.exists(selected_headless_file):
        try:
            with open(selected_headless_file, "r", encoding="utf-8") as f:
                current_headless = f.read().strip()
        except Exception:
            pass

    default_headless_choice = "n"
    if current_headless == "False":
        default_headless_choice = "y"

    prompt_suffix = " (Y/n)" if default_headless_choice == "y" else " (y/N)"
    try:
        headless_choice = input(f"Open browser window for visual monitoring?{prompt_suffix}: ").strip().lower()
        if not headless_choice:
            headless_choice = default_headless_choice

        if headless_choice in ('y', 'yes'):
            selected_headless = "False"
        else:
            selected_headless = "True"
    except Exception:
        selected_headless = "True"

    print(f"\n[SUCCESS] Browser visibility configured to: {'Visible (Headed)' if selected_headless == 'False' else 'Background (Headless)'}\n")

    # Save profile, model, thinking level and headless choices
    try:
        with open(selected_profile_file, "w", encoding="utf-8") as f:
            f.write(selected_profile)
        with open(SELECTED_MODEL_FILE, "w", encoding="utf-8") as f:
            f.write(selected_model.model_id)
        with open(selected_thinking_file, "w", encoding="utf-8") as f:
            f.write(selected_thinking)
        with open(selected_headless_file, "w", encoding="utf-8") as f:
            f.write(selected_headless)
    except Exception as e:
        print(f"Error saving choices to disk: {e}")
        sys.exit(1)
