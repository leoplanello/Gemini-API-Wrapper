import os
import json
from typing import Tuple, List, Optional

PROFILES_DIR = "profiles"
SELECTED_PROFILE_FILE = ".selected_profile"
SELECTED_MODEL_FILE = ".selected_model"
SELECTED_THINKING_FILE = ".thinking_level"


def get_active_profile(selected_profile_file: str = SELECTED_PROFILE_FILE) -> Optional[str]:
    """Gets the currently selected profile name."""
    if os.path.exists(selected_profile_file):
        try:
            with open(selected_profile_file, "r", encoding="utf-8") as f:
                prof = f.read().strip()
                if prof:
                    return prof
        except Exception:
            pass
    return None


def get_all_profiles(
    profiles_dir: str = PROFILES_DIR,
    selected_profile_file: str = SELECTED_PROFILE_FILE
) -> List[str]:
    """
    Scans the profiles directory and returns a list of profiles.
    The active profile is sorted to the front if it exists.
    """
    profiles = []
    if os.path.exists(profiles_dir) and os.path.isdir(profiles_dir):
        for item in os.listdir(profiles_dir):
            item_path = os.path.join(profiles_dir, item)
            if os.path.isdir(item_path):
                profiles.append(item)
    if not profiles:
        profiles.append("cookies")

    active_prof = get_active_profile(selected_profile_file)
    if active_prof and active_prof in profiles:
        profiles.sort(key=lambda p: 0 if p == active_prof else 1)
    return profiles


def resolve_paths(account: str, profiles_dir: str = PROFILES_DIR) -> Tuple[str, str, str]:
    """
    Resolve cookie file paths and profile folder from the account name.
    Returns (account_id, cookie_path, profile_path)
    """
    normalized = account.replace("\\", "/").strip()
    if normalized.endswith(".json"):
        normalized = normalized[:-5]
    if "/" in normalized:
        normalized = normalized.split("/")[-1]

    if normalized in ("cookies", "root", "default", "legacy"):
        account_id = "cookies"
    else:
        account_id = normalized

    profile_path = os.path.abspath(os.path.join(profiles_dir, account_id))
    cookie_path = os.path.abspath(os.path.join(profile_path, "cookies.json"))

    return account_id, cookie_path, profile_path


def load_cookies_from_json(filepath: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Load cookies __Secure-1PSID and __Secure-1PSIDTS from JSON file.
    Supports list format or flat dictionary format.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        psid, psidts = None, None
        if isinstance(data, list):
            for cookie in data:
                name = cookie.get("name")
                value = cookie.get("value")
                if name == "__Secure-1PSID":
                    psid = value
                elif name == "__Secure-1PSIDTS":
                    psidts = value
        elif isinstance(data, dict):
            for k, v in data.items():
                if k.lower() in ("__secure-1psid", "secure_1psid", "1psid"):
                    psid = v
                elif k.lower() in ("__secure-1psidts", "secure_1psidts", "1psidts"):
                    psidts = v
        return psid, psidts
    except Exception:
        return None, None


def save_cookies_to_json(filepath: str, psid: str, psidts: str):
    """
    Saves cookies to JSON in browser extension compatible format.
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    cookies_list = [
        {
            "name": "__Secure-1PSID",
            "value": psid,
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "httpOnly": True
        },
        {
            "name": "__Secure-1PSIDTS",
            "value": psidts,
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "httpOnly": False
        }
    ]
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(cookies_list, f, indent=2, ensure_ascii=False)


def get_active_model(selected_model_file: str = SELECTED_MODEL_FILE, default_model: str = "gemini-3-flash") -> str:
    """Gets the currently selected model ID."""
    if os.path.exists(selected_model_file):
        try:
            with open(selected_model_file, "r", encoding="utf-8") as f:
                model = f.read().strip()
                if model:
                    return model
        except Exception:
            pass
    return default_model


def get_active_thinking_level(selected_thinking_file: str = SELECTED_THINKING_FILE, default_level: str = "Standard") -> str:
    """Gets the currently selected thinking level ('Standard' or 'Extended')."""
    if os.path.exists(selected_thinking_file):
        try:
            with open(selected_thinking_file, "r", encoding="utf-8") as f:
                level = f.read().strip()
                if level:
                    return level
        except Exception:
            pass
    return default_level
