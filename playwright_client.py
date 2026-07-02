import sys
from pathlib import Path

# Add src/ to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi.playwright.exceptions import CaptchaError, LogoutError
from gemini_webapi.playwright.selectors import _SEND_BTN_SELECTOR, _INPUT_SELECTOR
from gemini_webapi.playwright.pool import (
    PlaywrightGeminiPool,
    playwright_pool,
    get_all_accounts,
)
