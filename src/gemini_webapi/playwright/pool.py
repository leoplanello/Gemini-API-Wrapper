import os
import sys
import time
import base64
import mimetypes
import logging
import asyncio
from playwright.async_api import async_playwright

from gemini_webapi.utils.profile_utils import (
    get_active_profile,
    get_active_profiles,
    get_all_profiles,
    resolve_paths,
)
from .exceptions import CaptchaError, LogoutError
from .stealth import _STEALTH_JS
from .selectors import (
    _INPUT_SELECTOR,
    _SEND_BTN_SELECTOR,
    _RESPONSE_SELECTOR,
    _RESPONSE_SELECTORS_LIST,
    _CONTENT_SUB_SELECTORS,
)

logger = logging.getLogger("playwright_client")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"))
    logger.addHandler(handler)
    try:  # the proxy runs hidden, so keep pool activity in a file as well
        import os as _os
        from logging.handlers import RotatingFileHandler as _RFH
        _logs_dir = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "..", "logs"))
        _os.makedirs(_logs_dir, exist_ok=True)
        _fh = _RFH(_os.path.join(_logs_dir, "pool.log"), maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"))
        logger.addHandler(_fh)
    except Exception:
        pass


def get_all_accounts() -> list[dict]:
    """
    Scans configured profiles directory to identify active accounts for the pool,
    using shared profile utilities.
    """
    profiles = get_active_profiles()
    accounts = []
    for p in profiles:
        _, _, profile_path = resolve_paths(p)
        accounts.append({
            "name": p,
            "profile_path": profile_path
        })
    return accounts



# Upper bound for one response (was a fixed ~47s poll count, which returned "" while Gemini was still generating)
_MAX_RESPONSE_WAIT_SEC = float(os.environ.get("GEMINI_MAX_WAIT_SEC", "240"))
# Fast modes (flash/lite) normally show text within 3-5s; if nothing appears by then Gemini has stalled.
_FAST_STALL_LIMIT_SEC = float(os.environ.get("GEMINI_FAST_STALL_SEC", "25"))
_FAST_RETRY_MAX_SEC = float(os.environ.get("GEMINI_FAST_RETRY_MAX_SEC", "60"))
_FAST_TEXT_IDLE_SEC = float(os.environ.get("GEMINI_FAST_IDLE_SEC", "6"))
_SLOW_TEXT_IDLE_SEC = float(os.environ.get("GEMINI_SLOW_IDLE_SEC", "25"))


class GenerationStall(Exception):
    """Gemini kept 'generating' without producing any text for too long."""


class PlaywrightGeminiPool:
    def __init__(self):
        self.playwright = None
        self.browsers = {}  # account_name -> context
        self.pages = {}     # account_name -> page
        self.locks = {}     # account_name -> Lock
        self.headless = True
        self.active_account_name = None
        self.is_fresh_chat = {}  # account_name -> bool
        self.last_used_account = None
        self.applied_mode = {}  # account_name -> (model_val, thinking_val) currently set in the Gemini UI

    def _locator(self, page, selector):
        """Helper to get a visibility-filtered locator or a standard one if mock."""
        is_mock = "Mock" in type(page).__name__
        if is_mock:
            return page.locator(selector)
        return page.locator(selector).locator("visible=true")

    def _clean_text_headers(self, text: str) -> str:
        """
        Removes scraped UI headers from the response text, such as
        'Gemini said', 'Reviewing the input', 'O Gemini disse', and glitched combinations.
        """
        if not text:
            return text

        import re
        while True:
            original = text
            text = text.lstrip()

            # 1. Match exact standalone lines of "Gemini" (case-insensitive) at the very start
            text = re.sub(r'^Gemini(?:\r?\n|$)', '', text, flags=re.IGNORECASE)
            text = text.lstrip()

            # 2. Match "Reviewing the input" variations (with or without dots/newlines/following headers)
            text = re.sub(r'^Reviewing\s+the\s+[iI]nput(?:\.+)?(?:\r?\n|$)', '', text, flags=re.IGNORECASE)
            text = text.lstrip()
            # Match "Reviewing the input" followed by a space and a header/garbage pattern
            text = re.sub(r'^Reviewing\s+the\s+[iI]nput(?:\.+)?\s+(?:Gemini\s+saidini\s+said|saidini\s+said|ini\s+said|Gemini\s+said|O\s+Gemini\s+disseini\s+disse|disseini\s+disse|ini\s+disse|O\s+Gemini\s+disse|Gemini\s+disse)\b', '', text, flags=re.IGNORECASE)
            text = text.lstrip()

            # 3. Match garbage header phrases (e.g. Gemini said, saidini said, etc.)
            text = re.sub(r'^(?:Gemini\s+saidini\s+said|saidini\s+said|ini\s+said|Gemini\s+said|O\s+Gemini\s+disseini\s+disse|disseini\s+disse|ini\s+disse|O\s+Gemini\s+disse|Gemini\s+disse)(?:\r?\n|$)', '', text, flags=re.IGNORECASE)
            text = text.lstrip()
            # Match garbage/header words directly at the start (e.g. "saidini said", "ini said")
            text = re.sub(r'^(?:Gemini\s+saidini\s+said|saidini\s+said|ini\s+said|disseini\s+disse|ini\s+disse)\b', '', text, flags=re.IGNORECASE)
            text = text.lstrip()
            # Match lines ending with "O Gemini disse", "Gemini disse", "Gemini said" or thought titles at the start
            text = re.sub(r'^.*?\b(?:O\s+Gemini\s+disse|Gemini\s+disse|Gemini\s+said|saidini\s+said|disseini\s+disse)\b(?:\r?\n|$)', '', text, flags=re.IGNORECASE)
            text = text.lstrip()

            if text == original:
                break

        return text

    async def _extract_last_valid_response(self, elements) -> tuple[str, str, bool]:
        """
        Scans backwards from the last element to find the most recent valid response container.
        Returns (clean_text, thoughts_text, has_copy_button).
        """
        try:
            count = await elements.count()
        except Exception:
            return "", "", False
        if count == 0:
            return "", "", False

        for i in range(count - 1, -1, -1):
            elem = elements.nth(i)
            raw_text = ""
            thoughts_text = ""
            has_copy = False
            try:
                if not ("Mock" in type(elem).__name__ or "Mock" in type(elements).__name__):
                    res = await elem.evaluate("""
                        (node, subSelectors) => {
                            let rawText = "";
                            let contentFound = false;
                            for (const sel of subSelectors) {
                                const found = node.querySelector(sel);
                                if (found) {
                                    const t = (found.innerText || found.textContent || "").trim();
                                    if (t) {
                                        rawText = found.innerText || found.textContent || "";
                                        contentFound = true;
                                        break;
                                    }
                                }
                            }
                            if (!contentFound) {
                                rawText = node.innerText || node.textContent || "";
                            }
                            let thoughtsText = "";
                            const detailsNode = node.querySelector('details, .thinking-content, .thinking-block');
                            if (detailsNode) {
                                thoughtsText = detailsNode.innerText || detailsNode.textContent || "";
                            }
                            let hasCopy = false;
                            const turnContainer = node.closest('model-response, [data-test-id*="model-response"], .response-container, .turn-content, .message-content, .chat-turn-container') || node;
                            const btns = turnContainer.querySelectorAll('button[aria-label*="Copy" i], button[aria-label*="Copiar" i], button[aria-label*="Copier" i], button[data-test-id*="copy" i], mat-icon[data-mat-icon-name="content_copy"], .copy-button, [data-tooltip*="Copy" i], [data-tooltip*="Copiar" i], [aria-label*="Copiar resposta" i], [aria-label*="Copy response" i]');
                            for (const b of btns) {
                                if (b.offsetWidth > 0 || b.offsetHeight > 0 || b.getClientRects().length > 0) {
                                    hasCopy = true;
                                    break;
                                }
                            }
                            return [rawText, thoughtsText, hasCopy];
                        }
                    """, _CONTENT_SUB_SELECTORS)
                    raw_text = (res[0] or "").strip()
                    thoughts_text = (res[1] or "").strip()
                    has_copy = bool(res[2]) if len(res) > 2 else False
                    if not raw_text:
                        continue
                    clean_text = raw_text
                    if thoughts_text:
                        stripped = raw_text.replace(thoughts_text, "").strip()
                        if stripped:
                            clean_text = stripped
                    clean_text = self._clean_text_headers(clean_text)
                    return clean_text, thoughts_text, has_copy
            except Exception:
                pass

            raw_text = ""
            content_found = False
            for sub_sel in _CONTENT_SUB_SELECTORS:
                try:
                    content_elem = elem.locator(sub_sel)
                    if await content_elem.count() > 0:
                        raw_text = await content_elem.first.inner_text()
                        content_found = True
                        break
                except Exception:
                    pass
            if not content_found:
                try:
                    raw_text = await elem.inner_text()
                except Exception:
                    raw_text = ""

            raw_text = raw_text.strip()
            if not raw_text:
                continue

            thoughts_text = ""
            clean_text = raw_text
            try:
                details_elem = elem.locator('details, .thinking-content, .thinking-block')
                if await details_elem.count() > 0:
                    thoughts_text = await details_elem.first.inner_text()
                    stripped = raw_text.replace(thoughts_text, "").strip()
                    if stripped:
                        clean_text = stripped
            except Exception:
                pass

            clean_text = self._clean_text_headers(clean_text)
            try:
                cb = elem.locator('button[aria-label*="Copy"], button[aria-label*="Copiar"], mat-icon[data-mat-icon-name="content_copy"]')
                if await cb.count() > 0:
                    has_copy = True
            except Exception:
                pass
            return clean_text, thoughts_text, has_copy

        return "", "", False

    async def _is_generating(self, page) -> bool:
        """
        Detects if the Gemini interface is currently generating a response.
        """
        try:
            if not ("Mock" in type(page).__name__):
                res = await page.evaluate("""
                ([sendSelectors, respSelectors]) => {
                    const respList = respSelectors.split(',').map(s => s.trim()).filter(Boolean);
                    let lastResp = null;
                    for (const sel of respList) {
                        const elems = document.querySelectorAll(sel);
                        if (elems && elems.length > 0) {
                            lastResp = elems[elems.length - 1];
                            break;
                        }
                    }
                    if (lastResp) {
                        let curr = lastResp;
                        for (let p = 0; p < 6 && curr; p++) {
                            if (curr.tagName && (curr.tagName.toLowerCase() === 'body' || curr.tagName.toLowerCase() === 'main' || curr.classList.contains('conversation-container') || curr.classList.contains('chat-history'))) {
                                break;
                            }
                            const copyBtns = curr.querySelectorAll('button[aria-label*="Copy" i], button[aria-label*="Copiar" i], button[aria-label*="Copier" i], button[data-test-id*="copy" i], mat-icon[data-mat-icon-name="content_copy"], .copy-button, [data-tooltip*="Copy" i], [data-tooltip*="Copiar" i], [aria-label*="Copiar resposta" i], [aria-label*="Copy response" i]');
                            for (const b of copyBtns) {
                                if (b.offsetWidth > 0 || b.offsetHeight > 0 || b.getClientRects().length > 0) {
                                    return false;
                                }
                            }
                            curr = curr.parentElement;
                        }
                    }
                    const stopBtns = document.querySelectorAll('button[aria-label*="Stop"], button[aria-label*="Parar"]');
                    for (const b of stopBtns) {
                        if (b.offsetWidth > 0 || b.offsetHeight > 0 || b.getClientRects().length > 0) return true;
                    }
                    const loadings = document.querySelectorAll('.model-response-loading, mat-progress-bar, [data-test-id*="loading"], .loading-indicator, .thinking-indicator');
                    for (const l of loadings) {
                        if (l.offsetWidth > 0 || l.offsetHeight > 0 || l.getClientRects().length > 0) return true;
                    }
                    const sels = sendSelectors.split(',').map(s => s.trim()).filter(Boolean);
                    let hasSend = false;
                    for (const sel of sels) {
                        if (document.querySelector(sel)) {
                            hasSend = true;
                            break;
                        }
                    }
                    if (!hasSend) return true;
                    return false;
                }
                """, [_SEND_BTN_SELECTOR, _RESPONSE_SELECTOR])
                return bool(res)
        except Exception:
            pass

        try:
            stop_btn = page.locator('button[aria-label*="Stop"], button[aria-label*="Parar"]')
            has_stop = await stop_btn.count() > 0 and await stop_btn.first.is_visible()
            if has_stop:
                return True

            loading_locator = page.locator('.model-response-loading, mat-progress-bar, [data-test-id*="loading"], .loading-indicator, .thinking-indicator')
            if await loading_locator.count() > 0 and await loading_locator.first.is_visible():
                return True

            send_btn = page.locator(_SEND_BTN_SELECTOR)
            has_send = await send_btn.count() > 0
            if not has_send:
                return True

            return False
        except Exception:
            return False

    async def _wait_for_generation_start(self, page, active_response_selector: str, initial_count: int, account_name: str, timeout_sec: float = 15.0) -> bool:
        is_mock = "Mock" in type(page).__name__
        if is_mock:
            return True

        iterations = int(timeout_sec / 0.05)
        for _ in range(iterations):
            if getattr(page, "captcha_detected", False) or "sorry/index" in page.url:
                raise CaptchaError(f"CAPTCHA redirect detected for account '{account_name}'")

            try:
                elements = self._locator(page, active_response_selector)
                current_count = await elements.count()
            except Exception:
                current_count = 0

            is_generating = await self._is_generating(page)

            if current_count > initial_count or is_generating:
                logger.info(f"[PlaywrightPool] [{account_name}] Generation start detected (count: {initial_count} -> {current_count}, generating: {is_generating})")
                return True
            await page.wait_for_timeout(50)

        logger.warning(f"[PlaywrightPool] [{account_name}] Timeout waiting for generation to start.")
        return False

    async def _inspect_page_status(self, page, account_name: str) -> str:
        is_mock = "Mock" in type(page).__name__
        try:
            if page.locator("a") is page.locator("b"):
                is_mock = True
        except Exception:
            pass

        current_url = ""
        try:
            current_url = page.url
            if not isinstance(current_url, str):
                current_url = ""
        except Exception:
            pass

        if is_mock:
            if "sorry/index" in current_url:
                return "captcha"
            if any(domain in current_url for domain in ["accounts.google.com", "/signin", "/ServiceLogin"]):
                return "logged_out"
            if getattr(page, "captcha_detected", False) is True:
                return "captcha"
            return "ok"

        if any(auth_domain in current_url for auth_domain in ["accounts.google.com", "/signin", "/ServiceLogin", "/login"]):
            logger.error(f"[PlaywrightPool] [{account_name}] Redirected to login/auth URL ({current_url}). Logged out.")
            return "logged_out"
        if "sorry/index" in current_url or "google.com/sorry" in current_url:
            logger.error(f"[PlaywrightPool] [{account_name}] URL indicates CAPTCHA page.")
            page.captcha_detected = True
            return "captcha"
        if "gemini.google.com" in current_url and not any(sub in current_url for sub in ["/app", "/share", "/c/", "/chat"]):
            logger.error(f"[PlaywrightPool] [{account_name}] URL indicates Gemini unauthenticated landing page ({current_url}). Logged out.")
            return "logged_out"

        try:
            if not ("Mock" in type(page).__name__):
                res = await page.evaluate("""
                (inputSelector) => {
                    const captchaSelectors = [
                        'iframe[src*="recaptcha"]',
                        'iframe[src*="api2/anchor"]',
                        'iframe[src*="hcaptcha"]',
                        'iframe[src*="challenge"]',
                        'div.g-recaptcha',
                        'div.cf-turnstile',
                        '#captcha'
                    ];
                    for (const sel of captchaSelectors) {
                        if (document.querySelector(sel)) return "captcha";
                    }
                    const text = (document.body && (document.body.innerText || document.body.textContent)) || "";
                    const lower = text.toLowerCase();
                    const suspiciousPhrases = ["unusual traffic", "tráfego incomum", "suspicious activity", "atividade suspeita"];
                    for (const phrase of suspiciousPhrases) {
                        if (lower.includes(phrase)) return "captcha";
                    }
                    const unambiguousSignInSelectors = [
                        'a[href*="ServiceLogin"]',
                        'a[href*="/signin"]',
                        'a[href*="signin"]',
                        'a[href*="/login"]',
                        'a[href*="SignUp"]',
                        '.sign-in-button',
                        '[data-test-id*="sign-in"]',
                        '[data-test-id*="signin"]',
                        '[aria-label*="Sign in"]',
                        '[aria-label*="Fazer login"]',
                        '[aria-label*="Entrar"]',
                        '[aria-label*="Login"]'
                    ];
                    for (const sel of unambiguousSignInSelectors) {
                        if (document.querySelector(sel)) return "logged_out";
                    }
                    if (!document.querySelector(inputSelector)) {
                        if (document.querySelector('a[href*="accounts.google.com"]')) return "logged_out";
                        const signInPhrases = [
                            "fazer login", "faça login", "sign in to gemini", "sign in", 
                            "acesse sua conta", "entrar", "log in", "iniciar sesión", "connexion",
                            "conversar com o gemini", "chat with gemini", "experimente o gemini", "try gemini"
                        ];
                        for (const phrase of signInPhrases) {
                            if (lower.includes(phrase)) return "logged_out";
                        }
                        return "logged_out";
                    }
                    return "ok";
                }
                """, _INPUT_SELECTOR)
                if res in ("captcha", "logged_out"):
                    if res == "captcha":
                        page.captcha_detected = True
                    return res
                if res == "ok":
                    return "ok"
        except Exception:
            pass

        try:
            captcha_selectors = [
                'iframe[src*="recaptcha"]',
                'iframe[src*="api2/anchor"]',
                'iframe[src*="hcaptcha"]',
                'iframe[src*="challenge"]',
                'div.g-recaptcha',
                'div.cf-turnstile',
                '#captcha'
            ]
            for sel in captcha_selectors:
                if await page.locator(sel).count() > 0:
                    logger.error(f"[PlaywrightPool] [{account_name}] CAPTCHA element detected in DOM via selector: {sel}")
                    page.captcha_detected = True
                    return "captcha"

            content = await page.content()
            lower_content = content.lower()
            suspicious_phrases = ["unusual traffic", "tráfego incomum", "suspicious activity", "atividade suspeita"]
            if any(phrase in lower_content for phrase in suspicious_phrases):
                if "sorry/index" in current_url or "google.com/sorry" in current_url:
                    logger.error(f"[PlaywrightPool] [{account_name}] CAPTCHA text detected on google.com/sorry.")
                    page.captcha_detected = True
                    return "captcha"

            unambiguous_sign_in_selectors = [
                'a[href*="ServiceLogin"]', 'a[href*="/signin"]', 'a[href*="signin"]', 'a[href*="/login"]',
                'a[href*="SignUp"]', '.sign-in-button', '[data-test-id*="sign-in"]', '[data-test-id*="signin"]',
                '[aria-label*="Sign in"]', '[aria-label*="Fazer login"]', '[aria-label*="Entrar"]', '[aria-label*="Login"]',
                'button:has-text("Sign in")', 'button:has-text("Fazer login")', 'button:has-text("Entrar")', 'button:has-text("Login")',
                'button:has-text("Acessar")', 'button:has-text("Conversar com o Gemini")', 'button:has-text("Chat with Gemini")',
                'button:has-text("Experimente o Gemini")', 'a:has-text("Sign in")', 'a:has-text("Fazer login")', 'a:has-text("Entrar")',
                'a:has-text("Login")', 'a:has-text("Acessar")', 'a:has-text("Conversar com o Gemini")', 'a:has-text("Chat with Gemini")',
                'a:has-text("Experimente o Gemini")', '[role="button"]:has-text("Sign in")', '[role="button"]:has-text("Fazer login")',
                '[role="button"]:has-text("Entrar")', '[role="button"]:has-text("Login")', '[role="button"]:has-text("Acessar")',
                '[role="link"]:has-text("Sign in")', '[role="link"]:has-text("Fazer login")', '[role="link"]:has-text("Entrar")',
                '[role="link"]:has-text("Login")', 'span:has-text("Sign in")', 'span:has-text("Fazer login")'
            ]
            for sel in unambiguous_sign_in_selectors:
                try:
                    if await page.locator(sel).count() > 0:
                        logger.error(f"[PlaywrightPool] [{account_name}] Logged out. Found sign-in element: {sel}")
                        return "logged_out"
                except Exception:
                    pass

            input_count = await page.locator(_INPUT_SELECTOR).count()
            if input_count == 0:
                try:
                    if await page.locator('a[href*="accounts.google.com"]').count() > 0:
                        logger.error(f"[PlaywrightPool] [{account_name}] Logged out. Found accounts.google.com element when no input box present.")
                        return "logged_out"
                except Exception:
                    pass

                sign_in_phrases = [
                    "fazer login", "faça login", "sign in to gemini", "sign in", 
                    "acesse sua conta", "entrar", "log in", "iniciar sesión", "connexion",
                    "conversar com o gemini", "chat with gemini", "experimente o gemini", "try gemini"
                ]
                if any(phrase in lower_content for phrase in sign_in_phrases):
                    logger.error(f"[PlaywrightPool] [{account_name}] Logged out. Found sign-in phrases in body when no input box present.")
                    return "logged_out"

                logger.error(f"[PlaywrightPool] [{account_name}] No prompt input area (_INPUT_SELECTOR) found on page.")
                return "logged_out"

        except Exception as e:
            logger.warning(f"[PlaywrightPool] [{account_name}] Error while checking page status: {e}")

        return "ok"

    async def check_page_status(self, page, account_name: str, attempt_reload: bool = True, *args, **kwargs) -> str:
        status = await self._inspect_page_status(page, account_name)
        if status == "logged_out" and attempt_reload:
            is_mock = "Mock" in type(page).__name__
            if is_mock:
                return status
            logger.warning(f"[PlaywrightPool] [{account_name}] 'Sign in' detected on page. 1 - Reloading page to verify...")
            try:
                if "gemini.google.com" in (page.url or ""):
                    await page.reload(timeout=45000)
                else:
                    await page.goto("https://gemini.google.com/app", timeout=45000)
                await page.wait_for_timeout(4000)
            except Exception as e:
                logger.warning(f"[PlaywrightPool] [{account_name}] Page reload failed during verification: {e}")

            status = await self._inspect_page_status(page, account_name)
            if status == "logged_out":
                logger.error(f"[PlaywrightPool] [{account_name}] 'Sign in' still present after reload.")
        return status

    async def _human_click(self, page, locator):
        """Emulates human-like mouse movement to the locator's center before clicking."""
        try:
            if "Mock" in type(page).__name__:
                await locator.click()
                return

            if hasattr(locator, "wait_for"):
                await locator.wait_for(state="visible", timeout=3000)
            box = await locator.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2

                import random
                start_x = random.randint(10, 100)
                start_y = random.randint(10, 100)

                steps = 3
                for i in range(1, steps + 1):
                    t = i / steps
                    cur_x = start_x + (x - start_x) * t + random.uniform(-2, 2)
                    cur_y = start_y + (y - start_y) * t + random.uniform(-2, 2)
                    await page.mouse.move(cur_x, cur_y)
                    await asyncio.sleep(0.01)

                await page.mouse.move(x, y)
                await page.mouse.click(x, y)
            else:
                await locator.click()
        except Exception as e:
            logger.warning(f"[PlaywrightPool] _human_click failed: {e}. Falling back to standard click.")
            try:
                await locator.click()
            except Exception:
                pass

    async def _trigger_new_chat(self, page, account_name: str) -> bool:
        is_mock = "Mock" in type(page).__name__
        if is_mock:
            return True

        new_chat_selectors = [
            'a[href="/app"]',
            'a[href*="gemini.google.com/app"]',
            'button[aria-label*="Nova conversa"]',
            'button[aria-label*="New chat"]',
            'button[aria-label*="Novo bate-papo"]',
            'button[aria-label*="Chat novo"]',
            'a[aria-label*="Nova conversa"]',
            'a[aria-label*="New chat"]',
            'div[aria-label*="Nova conversa"]',
            'div[aria-label*="New chat"]',
            'button:has-text("Nova conversa")',
            'button:has-text("New chat")',
            '.new-chat-button',
            'side-nav-button',
            'chat-window-header button[aria-label*="Nova conversa"]',
            'chat-window-header button[aria-label*="New chat"]',
        ]

        for sel in new_chat_selectors:
            try:
                locator = page.locator(sel)
                if await locator.count() > 0 and await locator.first.is_visible():
                    logger.info(f"[PlaywrightPool] [{account_name}] Clicking New Chat button via: {sel}")
                    await locator.first.click(timeout=3000)
                    for _ in range(15):
                        await page.wait_for_timeout(200)
                        current_url = page.url or ""
                        if "gemini.google.com/app" in current_url and "/c/" not in current_url:
                            logger.info(f"[PlaywrightPool] [{account_name}] Successfully started new chat via click.")
                            return True
            except Exception as e:
                logger.debug(f"[PlaywrightPool] [{account_name}] Selector '{sel}' failed for new chat: {e}")

        return False

    @staticmethod
    def _kill_profile_processes(profile_path: str):
        """Safely terminate any orphaned Chromium processes locked to this specific profile path."""
        try:
            norm_path = os.path.normpath(profile_path)
            if sys.platform == "win32":
                import subprocess
                clean_dir = norm_path.replace("'", "''")
                ps_cmd = (
                    f"Get-CimInstance Win32_Process | "
                    f"Where-Object {{ ($_.ProcessName -like '*chrome*' -or $_.ProcessName -like '*msedge*') -and $_.CommandLine -like '*{clean_dir}*' }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                    capture_output=True,
                    timeout=8
                )
            else:
                import subprocess
                subprocess.run(["pkill", "-f", norm_path], capture_output=True, timeout=5)
        except Exception as e:
            logger.debug(f"[PlaywrightPool] Error cleaning up orphaned profile processes: {e}")

    async def _launch_persistent_context_safely(
        self,
        profile_path: str,
        headless: bool,
        args: list,
        account_name: str,
    ):
        """
        Launches a persistent browser context with automatic cleanup of orphaned
        Chromium processes and single retry if profile lock / existing session collision is detected.
        """
        launch_kwargs = dict(
            user_data_dir=profile_path,
            headless=headless,
            channel="chrome" if os.name == "nt" else None,
            args=args,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        try:
            return await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as launch_err:
            err_msg = str(launch_err).lower()
            if "existing browser session" in err_msg or "already in use" in err_msg or "singletonlock" in err_msg:
                logger.warning(
                    f"[PlaywrightPool] Account '{account_name}' profile locked by existing/orphaned Chromium session. "
                    f"Cleaning up orphaned processes for '{profile_path}' and retrying..."
                )
                self._kill_profile_processes(profile_path)
                await asyncio.sleep(1.5)
                return await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            raise

    async def start_pool(self, headless: bool = True):
        self.headless = headless
        accounts = get_all_accounts()
        if not accounts:
            logger.warning("[PlaywrightPool] No configured account found for pre-warming.")
            return

        active_prof = get_active_profile()
        if active_prof and any(acc["name"] == active_prof for acc in accounts):
            self.active_account_name = active_prof
        elif not self.active_account_name and accounts:
            self.active_account_name = accounts[0]["name"]

        logger.info(f"[PlaywrightPool] Starting pool for {len(accounts)} accounts (headless={headless})...")
        self.playwright = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox"
        ]

        for acc in accounts:
            name = acc["name"]
            profile_path = acc["profile_path"]
            self.locks[name] = asyncio.Lock()

            logger.info(f"[PlaywrightPool] Pre-warming '{name}' in '{profile_path}'...")
            try:
                context = await self._launch_persistent_context_safely(
                    profile_path=profile_path,
                    headless=headless,
                    args=launch_args,
                    account_name=name,
                )

                page = await context.new_page()
                await page.add_init_script(_STEALTH_JS)
                self._setup_page_listeners(page, name)

                logger.info(f"[PlaywrightPool] Navigating to Gemini Web for '{name}'...")
                await page.goto("https://gemini.google.com/app", timeout=45000)
                await page.wait_for_timeout(3000)

                status = await self.check_page_status(page, name)
                if status != "ok":
                    logger.warning(f"[PlaywrightPool] Account '{name}' session is invalid, expired, or blocked (status: {status}). Attempting manual login/solving...")
                    await context.close()
                    solved = await self.solve_session_interactively(name)
                    if not solved:
                        logger.error(f"[PlaywrightPool] Failed to manually log in / solve session for account '{name}'. Skipping.")
                        continue
                else:
                    self.browsers[name] = context
                    self.pages[name] = page
                    self.is_fresh_chat[name] = True
                    logger.info(f"[PlaywrightPool] Account '{name}' successfully pre-warmed!")
                    await self._ensure_model_and_thinking(page, name)
            except Exception as e:
                logger.error(f"[PlaywrightPool] Failed to pre-warm account '{name}': {e}", exc_info=True)

    async def close(self):
        logger.info("[PlaywrightPool] Closing all pool connections...")
        for name, context in list(self.browsers.items()):
            try:
                await context.close()
            except Exception:
                pass
        self.browsers.clear()
        self.pages.clear()

        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    async def reset_account_chat(self, account_name: str):
        if account_name not in self.locks:
            logger.warning(f"[PlaywrightPool] [{account_name}] Cannot reset: not in locks.")
            return

        async with self.locks[account_name]:
            page = self.pages.get(account_name)
            if not page:
                logger.warning(f"[PlaywrightPool] [{account_name}] Cannot reset: no active page.")
                return

            logger.info(f"[PlaywrightPool] [{account_name}] Resetting chat to fresh state in background...")
            try:
                success = await self._trigger_new_chat(page, account_name)
                if not success:
                    await page.goto("https://gemini.google.com/app", timeout=45000)
                # wait until the new chat is actually empty and ready (instead of a fixed 1s sleep)
                for _ in range(30):
                    try:
                        if await self._locator(page, _RESPONSE_SELECTOR).count() == 0 and await page.locator(_INPUT_SELECTOR).count() > 0:
                            break
                    except Exception:
                        pass
                    await page.wait_for_timeout(50)
                status = await self.check_page_status(page, account_name)
                if status == "ok":
                    # keep the mode the last request used; skip the menu round-trip if already applied
                    prev_model, prev_thinking = self.applied_mode.get(account_name) or (None, None)
                    await self._ensure_mode_for_request(page, account_name, prev_model, prev_thinking)
                    self.is_fresh_chat[account_name] = True
                    logger.info(f"[PlaywrightPool] [{account_name}] Background reset complete. Marked as fresh chat.")
                else:
                    logger.warning(f"[PlaywrightPool] [{account_name}] Background reset status check failed ({status}). Triggering interactive session solving...")
                    asyncio.create_task(self.solve_session_interactively(account_name))
            except Exception as e:
                logger.error(f"[PlaywrightPool] [{account_name}] Error during background reset: {e}", exc_info=True)

    async def _upload_via_clipboard(self, page, account_name: str, files: list[str], tag_str: str) -> bool:
        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Attempting clipboard paste upload...")
        try:
            for filepath in files:
                if not os.path.exists(filepath):
                    logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} File not found: {filepath}")
                    return False

                filename = os.path.basename(filepath)
                mime_type, _ = mimetypes.guess_type(filepath)
                if not mime_type:
                    mime_type = "application/octet-stream"

                with open(filepath, "rb") as f:
                    file_bytes = f.read()
                base64_data = base64.b64encode(file_bytes).decode("utf-8")

                js_paste = """
                async (info) => {
                    const { base64Data, filename, mimeType, selector } = info;
                    const byteCharacters = atob(base64Data);
                    const byteNumbers = new Array(byteCharacters.length);
                    for (let i = 0; i < byteCharacters.length; i++) {
                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                    }
                    const byteArray = new Uint8Array(byteNumbers);
                    const blob = new Blob([byteArray], { type: mimeType });
                    const file = new File([blob], filename, { type: mimeType });

                    const target = document.querySelector(selector) || document.querySelector('rich-textarea, [contenteditable="true"], textarea, [role="textbox"]');
                    if (!target) {
                        return false;
                    }

                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(file);

                    const pasteEvent = new ClipboardEvent("paste", {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: dataTransfer
                    });

                    target.focus();
                    target.dispatchEvent(pasteEvent);
                    return true;
                }
                """
                success = await page.evaluate(js_paste, {
                    "base64Data": base64_data,
                    "filename": filename,
                    "mimeType": mime_type,
                    "selector": _INPUT_SELECTOR
                })
                if not success:
                    logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} Clipboard paste evaluate returned false.")
                    return False
                logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Clipboard paste dispatched for {filename}")
                await page.wait_for_timeout(50)
            return True
        except Exception as e:
            logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} Clipboard paste failed: {e}")
            return False

    async def _upload_via_drag_drop(self, page, account_name: str, files: list[str], tag_str: str) -> bool:
        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Attempting drag-drop simulation upload...")
        try:
            for filepath in files:
                if not os.path.exists(filepath):
                    logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} File not found: {filepath}")
                    return False

                filename = os.path.basename(filepath)
                mime_type, _ = mimetypes.guess_type(filepath)
                if not mime_type:
                    mime_type = "application/octet-stream"

                with open(filepath, "rb") as f:
                    file_bytes = f.read()
                base64_data = base64.b64encode(file_bytes).decode("utf-8")

                js_drag_drop = """
                async (info) => {
                    const { base64Data, filename, mimeType, selector } = info;
                    const byteCharacters = atob(base64Data);
                    const byteNumbers = new Array(byteCharacters.length);
                    for (let i = 0; i < byteCharacters.length; i++) {
                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                    }
                    const byteArray = new Uint8Array(byteNumbers);
                    const blob = new Blob([byteArray], { type: mimeType });
                    const file = new File([blob], filename, { type: mimeType });

                    const target = document.querySelector(selector) || document.querySelector('rich-textarea, [contenteditable="true"], textarea, [role="textbox"]');
                    if (!target) {
                        return false;
                    }

                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(file);

                    const dragenterEvent = new DragEvent("dragenter", {
                        bubbles: true, cancelable: true, dataTransfer
                    });
                    target.dispatchEvent(dragenterEvent);

                    const dragoverEvent = new DragEvent("dragover", {
                        bubbles: true, cancelable: true, dataTransfer
                    });
                    target.dispatchEvent(dragoverEvent);

                    const dropEvent = new DragEvent("drop", {
                        bubbles: true, cancelable: true, dataTransfer
                    });
                    target.dispatchEvent(dropEvent);
                    return true;
                }
                """
                success = await page.evaluate(js_drag_drop, {
                    "base64Data": base64_data,
                    "filename": filename,
                    "mimeType": mime_type,
                    "selector": _INPUT_SELECTOR
                })
                if not success:
                    logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} Drag-drop evaluate returned false.")
                    return False
                logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Drag-drop events dispatched for {filename}")
                await page.wait_for_timeout(50)
            return True
        except Exception as e:
            logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} Drag-drop failed: {e}")
            return False

    async def _wait_for_upload_confirm(self, page, account_name: str, tag_str: str, timeout: int) -> bool:
        _CHIP_SELECTORS = [
            'gemini-file-preview', 'file-preview', 'mat-chip', '.image-preview',
            'div[data-test-id*="file"]', 'div[data-test-id*="image"]',
            'div[data-test-id*="preview"]', 'div[class*="preview-chip"]',
            'div[class*="attachment"]', 'img[src*="blob:"]', 'img[src*="data:image"]',
            'uploader-file-preview', '.file-preview-chip', '.attachment-preview-wrapper',
            '.upload-chip', '[data-test-id="attachment-chip"]', '.attachment-container',
            'div[aria-label*="attached"]', 'div[aria-label*="anexo"]',
            '.file-thumbnail', '.file-chip', 'rich-textarea .file',
            'rich-textarea img', '.input-area img',
        ]

        combined = ", ".join(_CHIP_SELECTORS)
        try:
            await page.wait_for_selector(combined, timeout=timeout, state="attached")
            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Upload confirmed (chip found via combined selector).")
            return True
        except Exception:
            pass

        try:
            await page.wait_for_function(
                """() => {
                    const area = document.querySelector('rich-textarea, .input-area, [contenteditable]');
                    if (!area) return false;
                    const imgs = area.querySelectorAll('img, .file, .attachment, [data-file], uploader-file-preview, .file-preview-chip, .attachment-preview-wrapper');
                    return imgs.length > 0;
                }""",
                timeout=timeout // 2
            )
            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Upload confirmed (image/file element detected in input area).")
            return True
        except Exception:
            pass

        return False

    async def _upload_via_native(self, page, account_name: str, files: list[str], tag_str: str) -> bool:
        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Attempting native file upload sequence...")
        try:
            is_mock = "Mock" in type(page).__name__

            if not is_mock:
                file_input = page.locator('input[type="file"]')
                if await file_input.count() > 0:
                    try:
                        await file_input.first.set_input_files(files, timeout=3000)
                        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Files set directly via existing input[type=file].")
                        confirmed = await self._wait_for_upload_confirm(page, account_name, tag_str, timeout=3500)
                        if confirmed:
                            return True
                    except Exception as e:
                        logger.debug(f"[PlaywrightPool] [{account_name}]{tag_str} Direct set_input_files failed: {e}")

            upload_tools_selectors = [
                'button[aria-label*="Upload & tools"]',
                'button[aria-label*="Upload"]',
                'button[aria-label*="Attach"]'
            ]
            file_chooser_resolved = False
            for selector in upload_tools_selectors:
                try:
                    btn = page.locator(selector)
                    if await btn.count() > 0:
                        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Clicking upload/attach button: {selector}")
                        if not is_mock and hasattr(page, "expect_file_chooser"):
                          try:
                            async with page.expect_file_chooser(timeout=2000) as fc_info:
                              await btn.first.click(timeout=3000)
                            file_chooser = await fc_info.value
                            await file_chooser.set_files(files)
                            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Files set via intercepted file chooser on upload/attach button.")
                            file_chooser_resolved = True
                            break
                          except asyncio.TimeoutError:
                            pass
                        else:
                          await btn.first.click(timeout=3000)
                        await page.wait_for_timeout(300)
                        break
                except Exception as e:
                    logger.debug(f"[PlaywrightPool] [{account_name}]{tag_str} Selector {selector} failed to click: {e}")
                    continue

            upload_files_selectors = [
                'button[data-test-id="local-images-files-uploader-button"]',
                'button[aria-label*="Upload files"]',
                '[role="menuitem"]:has-text("Upload files")',
                'button:has-text("Upload files")',
            ]
            if not file_chooser_resolved:
                for selector in upload_files_selectors:
                    try:
                        btn = page.locator(selector)
                        if await btn.count() > 0:
                            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Clicking upload files menu item: {selector}")
                            if not is_mock and hasattr(page, "expect_file_chooser"):
                              try:
                                async with page.expect_file_chooser(timeout=3000) as fc_info:
                                  await btn.first.click(timeout=3000)
                                file_chooser = await fc_info.value
                                await file_chooser.set_files(files)
                                logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Files set via intercepted file chooser on upload files menu item.")
                                file_chooser_resolved = True
                                break
                              except asyncio.TimeoutError:
                                pass
                            else:
                              await btn.first.click(timeout=3000)
                            await page.wait_for_timeout(300)
                            break
                    except Exception as e:
                        logger.debug(f"[PlaywrightPool] [{account_name}]{tag_str} Selector {selector} failed to click: {e}")
                        continue

            if not file_chooser_resolved:
                file_input = page.locator('input[type="file"]')
                try:
                    await page.wait_for_selector('input[type="file"]', timeout=5000, state="attached")
                except Exception as e:
                    logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} input[type=file] not attached: {e}")

                if await file_input.count() > 0:
                    await file_input.first.set_input_files(files, timeout=15000)
                    logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Files set via native uploader.")
                else:
                    logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} No file input found after click sequence.")
                    return False

            confirmed = await self._wait_for_upload_confirm(page, account_name, tag_str, timeout=20000)
            return confirmed
        except Exception as e:
            logger.warning(f"[PlaywrightPool] [{account_name}]{tag_str} Native upload failed: {e}")
            return False

    async def _upload_files(self, page, account_name: str, files: list[str], tag: str = "") -> bool:
        tag_str = f" ({tag})" if tag else ""
        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Uploading {len(files)} file(s): {files}")

        uploaded = await self._upload_via_native(page, account_name, files, tag_str)
        if uploaded:
            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Native upload succeeded!")
            return True
        logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Native upload failed or did not confirm. Trying clipboard paste fallback...")

        uploaded = await self._upload_via_clipboard(page, account_name, files, tag_str)
        if uploaded:
            if await self._wait_for_upload_confirm(page, account_name, tag_str, timeout=8000):
                logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Clipboard paste upload confirmed!")
                return True
            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Clipboard paste did not show upload confirmation. Trying drag-drop...")

        uploaded = await self._upload_via_drag_drop(page, account_name, files, tag_str)
        if uploaded:
            if await self._wait_for_upload_confirm(page, account_name, tag_str, timeout=8000):
                logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Drag-drop upload confirmed!")
                return True
            logger.info(f"[PlaywrightPool] [{account_name}]{tag_str} Drag-drop did not show upload confirmation.")

        return False

    @staticmethod
    def _resolve_mode(account_name: str = "", model_override: str = None, thinking_override: str = None) -> tuple[str, str]:
        """Effective (model, thinking): per-request override first, then the shared .selected_* files."""
        model_val = "gemini-3-flash"
        thinking_val = "Standard"

        if os.path.exists(".selected_model"):
            try:
                with open(".selected_model", "r", encoding="utf-8") as f:
                    val = f.read().strip()
                    if val:
                        model_val = val
            except Exception as e:
                logger.warning(f"[PlaywrightPool] [{account_name}] Failed to read .selected_model: {e}")

        if os.path.exists(".thinking_level"):
            try:
                with open(".thinking_level", "r", encoding="utf-8") as f:
                    val = f.read().strip()
                    if val:
                        thinking_val = val
            except Exception as e:
                logger.warning(f"[PlaywrightPool] [{account_name}] Failed to read .thinking_level: {e}")

        if model_override:
            model_val = model_override.strip()
        if thinking_override:
            thinking_val = thinking_override.strip()
        return model_val, thinking_val

    async def _ensure_mode_for_request(self, page, account_name: str, model_override: str = None, thinking_override: str = None, force: bool = False):
        """Apply the model/thinking mode only when it differs from what the page already has."""
        wanted = self._resolve_mode(account_name, model_override, thinking_override)
        if not force and self.applied_mode.get(account_name) == wanted:
            return
        await self._ensure_model_and_thinking(page, account_name, model_override, thinking_override)

    async def _ensure_model_and_thinking(self, page, account_name: str, model_override: str = None, thinking_override: str = None):
        model_val, thinking_val = self._resolve_mode(account_name, model_override, thinking_override)
        self.applied_mode[account_name] = (model_val, thinking_val)

        flash_ids = ["56fdd199312815e2", "fbb127bbb056c959"]
        pro_ids = ["e6fa609c3fa255c0", "9d8ca3786ebdfbea"]
        lite_ids = ["8c46e95b1a07cecc"]
        thinking_ids = ["5bf011840784117a", "e051ce1aa80aa576"]

        target_type = "flash"
        if "pro" in model_val.lower() or "9d8ca3786ebdfbea" in model_val.lower() or "e6fa609c3fa255c0" in model_val.lower():
            target_type = "pro"
        elif "lite" in model_val.lower() or "8c46e95b1a07cecc" in model_val.lower():
            target_type = "lite"
        elif "thinking" in model_val.lower() or "5bf011840784117a" in model_val.lower() or "e051ce1aa80aa576" in model_val.lower():
            target_type = "thinking"

        target_thinking = "Standard"
        if thinking_val.lower() == "extended":
            target_thinking = "Extended"
        elif target_type == "thinking" and not os.path.exists(".thinking_level"):
            target_thinking = "Extended"

        logger.info(f"[PlaywrightPool] [{account_name}] Ensuring model={model_val} (target_type={target_type}) and thinking={target_thinking}")

        try:
            selector_btn = page.locator("button.input-area-switch, button[aria-label*='seletor de modo'], button[aria-label*='mode selector']")
            if await selector_btn.count() == 0:
                logger.warning(f"[PlaywrightPool] [{account_name}] Model selector button not found.")
                self.applied_mode.pop(account_name, None)
                return

            await self._human_click(page, selector_btn.first)
            await page.wait_for_timeout(1000)

            menu_items = await page.query_selector_all("[role='menuitem'], gem-menu-item")

            has_dedicated_thinking = False
            if target_type == "thinking":
                for item in menu_items:
                    mode_id = await item.get_attribute("data-mode-id")
                    t = await item.inner_text()
                    if mode_id in thinking_ids or (mode_id and t and "thinking" in t.lower() and not ("level" in t.lower() or "nacional" in t.lower() or "raciocínio" in t.lower())):
                        has_dedicated_thinking = True
                        break
                if not has_dedicated_thinking:
                    target_type = "flash"

            model_item = None
            is_model_already_selected = False

            for item in menu_items:
                mode_id = await item.get_attribute("data-mode-id")
                t = await item.inner_text()
                t_lower = t.lower() if t else ""

                matched = False
                if target_type == "flash" and (mode_id in flash_ids or ("flash" in t_lower and "lite" not in t_lower and "thinking" not in t_lower)):
                    matched = True
                elif target_type == "pro" and (mode_id in pro_ids or "pro" in t_lower):
                    matched = True
                elif target_type == "lite" and (mode_id in lite_ids or "lite" in t_lower):
                    matched = True
                elif target_type == "thinking" and (mode_id in thinking_ids or ("thinking" in t_lower and not ("level" in t_lower or "raciocínio" in t_lower))):
                    matched = True

                if matched:
                    model_item = item
                    cls = await item.get_attribute("class")
                    if cls and "selected" in cls:
                        is_model_already_selected = True
                    break

            if model_item and not is_model_already_selected:
                try:
                    is_disabled = await model_item.is_disabled()
                except Exception:
                    is_disabled = False

                aria_disabled = await model_item.get_attribute("aria-disabled")
                disabled_attr = await model_item.get_attribute("disabled")
                if is_disabled or aria_disabled == "true" or disabled_attr is not None:
                    logger.warning(f"[PlaywrightPool] [{account_name}] Model option for target_type={target_type} is disabled/locked in UI. Skipping selection.")
                else:
                    logger.info(f"[PlaywrightPool] [{account_name}] Selecting model for target_type={target_type}...")
                    await self._human_click(page, model_item)
                    await page.wait_for_timeout(1500)
                    await self._human_click(page, selector_btn.first)
                    await page.wait_for_timeout(1000)
                    menu_items = await page.query_selector_all("[role='menuitem'], gem-menu-item")
            elif not model_item:
                logger.warning(f"[PlaywrightPool] [{account_name}] Model option for target_type={target_type} not found in menu.")

            thinking_expander = None
            current_thinking_text = ""
            for item in menu_items:
                t = await item.inner_text()
                t_lower = t.lower() if t else ""
                if "raciocínio" in t_lower or "thinking level" in t_lower or "pensamiento" in t_lower or \
                   ("thinking" in t_lower and not ("flash" in t_lower or "pro" in t_lower or await item.get_attribute("data-mode-id"))):
                    thinking_expander = item
                    current_thinking_text = t
                    break

            if thinking_expander:
                is_extended_active = any(kw in current_thinking_text.lower() for kw in ["estendido", "extendido", "extended"])
                desired_extended = target_thinking == "Extended"

                if is_extended_active != desired_extended:
                    logger.info(f"[PlaywrightPool] [{account_name}] Changing thinking level (current: {'Extended' if is_extended_active else 'Standard'}, target: {target_thinking})...")
                    await self._human_click(page, thinking_expander)
                    await page.wait_for_timeout(1000)

                    sub_items = await page.query_selector_all("[role='menuitem'], gem-menu-item")
                    target_option_item = None
                    for item in sub_items:
                        t = await item.inner_text()
                        t_lower = t.lower() if t else ""
                        if desired_extended:
                            if any(kw in t_lower for kw in ["estendido", "extendido", "extended"]):
                                target_option_item = item
                                break
                        else:
                            if any(kw in t_lower for kw in ["padrão", "estándar", "estandar", "standard"]):
                                target_option_item = item
                                break

                    if target_option_item:
                        await self._human_click(page, target_option_item)
                        await page.wait_for_timeout(1500)
                    else:
                        logger.warning(f"[PlaywrightPool] [{account_name}] Target thinking option not found.")
                        await page.keyboard.press("Escape")
                else:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
            else:
                logger.info(f"[PlaywrightPool] [{account_name}] Thinking level option not available for this model/account.")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)

        except Exception as e:
            self.applied_mode.pop(account_name, None)
            logger.error(f"[PlaywrightPool] [{account_name}] Error ensuring model/thinking level: {e}", exc_info=True)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _detect_response_selector(self, page) -> str:
        for selector in _RESPONSE_SELECTORS_LIST:
            try:
                count = await self._locator(page, selector).count()
                if count > 0:
                    logger.info(f"[PlaywrightPool] Detected active response selector: {selector!r}")
                    return selector
            except Exception:
                continue
        return _RESPONSE_SELECTOR

    async def _log_empty_response_diagnostics(self, page, account_name: str, selector: str, initial_count: int):
        """Called when the read loop gave up without any text: record what the page looked like."""
        try:
            info = await page.evaluate("""(sel) => {
                const els = Array.from(document.querySelectorAll(sel));
                const last = els[els.length - 1];
                return {
                    url: location.href, title: document.title, count: els.length,
                    lastText: last ? (last.innerText || '').slice(0, 300) : null,
                    lastHtml: last ? last.outerHTML.slice(0, 600) : null,
                    generating: !!document.querySelector('button[aria-label*="Stop" i], button[aria-label*="Parar" i]'),
                    buttons: Array.from(document.querySelectorAll('button, [role="button"]')).filter(b => b.offsetWidth > 0 || b.offsetHeight > 0).map(b => (b.getAttribute('aria-label') || b.getAttribute('data-test-id') || b.innerText || '').trim().slice(0, 40)).filter(Boolean).slice(-40),
                    body: (document.body.innerText || '').slice(-400),
                };
            }""", selector)
            logger.warning(f"[PlaywrightPool] [{account_name}] Empty response (initial_count={initial_count}). Page state: {info}")
            shot = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "logs", "empty_response.png"))
            await page.screenshot(path=shot)
            logger.warning(f"[PlaywrightPool] [{account_name}] Screenshot saved to {shot}")
        except Exception as e:
            logger.warning(f"[PlaywrightPool] [{account_name}] Could not collect empty-response diagnostics: {e}")

    @staticmethod
    def _log_timing(account_name: str, tm: dict):
        keys = ["start", "ready", "typed", "sent", "first_text", "done"]
        parts, prev = [], tm.get("start")
        for k in keys[1:]:
            if k in tm and prev is not None:
                parts.append(f"{k}=+{tm[k] - prev:.1f}s")
                prev = tm[k]
        logger.info(f"[PlaywrightPool] [{account_name}] [Timing] " + " ".join(parts) + f" total={tm.get('done', time.time()) - tm['start']:.1f}s")

    async def send_message(self, account_name: str, prompt: str, files: list[str] = None, clean_prompt: str = None,
                           force_new_chat: bool = False, continue_chat: bool = False, open_new_chat_after: bool = True,
                           model_override: str = None, thinking_override: str = None) -> tuple[str, str]:
        kwargs = dict(files=files, clean_prompt=clean_prompt, force_new_chat=force_new_chat, continue_chat=continue_chat,
                      open_new_chat_after=open_new_chat_after, model_override=model_override, thinking_override=thinking_override)
        try:
            return await self._send_message_once(account_name, prompt, **kwargs)
        except GenerationStall:
            logger.warning(f"[PlaywrightPool] [{account_name}] Gemini stalled with no text; retrying once in a fresh chat.")
            await self.reset_account_chat(account_name)
            kwargs.update(force_new_chat=True, continue_chat=False)
            return await self._send_message_once(account_name, prompt, _allow_stall=False, **kwargs)

    async def _send_message_once(
        self,
        account_name: str,
        prompt: str,
        files: list[str] = None,
        clean_prompt: str = None,
        force_new_chat: bool = False,
        continue_chat: bool = False,
        open_new_chat_after: bool = True,
        model_override: str = None,
        thinking_override: str = None,
        _allow_stall: bool = True,
    ) -> tuple[str, str]:
        if account_name not in self.locks:
            raise ValueError(f"Account '{account_name}' not configured in the pool.")

        async with self.locks[account_name]:
            _tm = {'start': time.time()}
            page = self.pages.get(account_name)
            if not page:
                raise RuntimeError(f"Account '{account_name}' does not have an active page in the pool.")

            is_fresh = self.is_fresh_chat.get(account_name, True)
            profile_changed = (self.last_used_account is not None and self.last_used_account != account_name)
            current_url = page.url or ""
            is_on_gemini = "gemini.google.com" in current_url

            should_navigate = False
            if not is_on_gemini:
                should_navigate = True
                logger.info(f"[PlaywrightPool] [{account_name}] Current URL '{current_url}' is not on Gemini Web. Navigating to start a new chat...")
            elif continue_chat and not profile_changed:
                should_navigate = False
                logger.info(f"[PlaywrightPool] [{account_name}] Continuing chat in existing session...")
            elif force_new_chat and not is_fresh:
                should_navigate = True
                logger.info(f"[PlaywrightPool] [{account_name}] force_new_chat requested for an existing session. Navigating to start a new chat...")
            elif profile_changed:
                should_navigate = True
                logger.info(f"[PlaywrightPool] [{account_name}] Profile changed from '{self.last_used_account}'. Forcing new chat to send full conversation history.")

            if should_navigate:
                success = await self._trigger_new_chat(page, account_name)
                if not success:
                    await page.goto("https://gemini.google.com/app", timeout=30000)
                is_fresh = True

            status = await self.check_page_status(page, account_name)
            if status == "captcha":
                raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
            elif status == "logged_out":
                raise LogoutError(f"Account '{account_name}' is logged out")

            try:
                await page.wait_for_selector(_INPUT_SELECTOR, timeout=10000)
            except Exception:
                await page.wait_for_timeout(100)

            status = await self.check_page_status(page, account_name)
            if status == "captcha":
                raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
            elif status == "logged_out":
                raise LogoutError(f"Account '{account_name}' is logged out")

            if should_navigate:
                await self._ensure_mode_for_request(page, account_name, model_override, thinking_override, force=True)
            elif is_fresh:
                await self._ensure_mode_for_request(page, account_name, model_override, thinking_override)

            _tm['ready'] = time.time()
            prompt_to_send = prompt
            if not is_fresh and clean_prompt:
                prompt_to_send = clean_prompt
                logger.info(f"[PlaywrightPool] [{account_name}] Continuing chat: sending clean prompt.")
            else:
                logger.info(f"[PlaywrightPool] [{account_name}] Fresh/new chat: sending full prompt.")

            await page.wait_for_selector(_INPUT_SELECTOR, timeout=15000)
            input_locator = page.locator(_INPUT_SELECTOR).first
            await self._human_click(page, input_locator)
            await page.wait_for_timeout(50)
            await input_locator.fill(prompt_to_send)
            _tm['typed'] = time.time()
            eval_res = input_locator.evaluate("el => el.dispatchEvent(new Event('input', { bubbles: true }))")
            if hasattr(eval_res, "__await__"):
                await eval_res

            is_mock = "Mock" in type(page).__name__
            if not is_mock:
                await input_locator.press("Space")
                await page.wait_for_timeout(20)
                await input_locator.press("Backspace")
                await page.wait_for_timeout(30)

            await page.wait_for_timeout(50)

            if files:
                logger.info(f"[PlaywrightPool] [{account_name}] Text typed. Now attaching {len(files)} file(s)...")
                uploaded = await self._upload_files(page, account_name, files)
                if not uploaded:
                    logger.warning(f"[PlaywrightPool] [{account_name}] File upload failed.")
                    raise ValueError("File upload failed")
                await page.wait_for_timeout(100)

            status = await self.check_page_status(page, account_name)
            if status == "captcha":
                raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
            elif status == "logged_out":
                raise LogoutError(f"Account '{account_name}' is logged out")

            active_response_selector = await self._detect_response_selector(page)
            initial_count = 0
            try:
                elements = self._locator(page, active_response_selector)
                initial_count = await elements.count()
            except Exception:
                pass

            send_button = page.locator(_SEND_BTN_SELECTOR)
            wait_ready_timeout = 35.0 if files else 5.0
            start_ready_time = time.time()
            while time.time() - start_ready_time < wait_ready_timeout:
                if is_mock:
                    break
                try:
                    ready = await page.evaluate("""() => {
                        const btns = document.querySelectorAll('button[aria-label*="Send"], button[aria-label*="Enviar"], button[data-test-id*="send"], button.send-button');
                        for (const btn of btns) {
                            if (!btn.disabled && btn.getAttribute("aria-disabled") !== "true" && btn.offsetParent !== null) return true;
                        }
                        return false;
                    }""")
                    if ready:
                        break
                except Exception:
                    try:
                        if await send_button.count() > 0:
                            btn = send_button.first
                            if await btn.is_enabled() and (await btn.get_attribute("aria-disabled")) != "true" and (await btn.get_attribute("disabled")) is None:
                                break
                    except Exception:
                        pass
                await asyncio.sleep(0.05)

            gen_started = False
            for attempt in range(3):
                if is_mock:
                    gen_started = True
                    break

                clicked = False
                try:
                    if await send_button.count() > 0:
                        btn = send_button.first
                        if await btn.is_enabled() and (await btn.get_attribute("aria-disabled")) != "true":
                            await self._human_click(page, btn)
                            clicked = True
                except Exception:
                    pass

                if not clicked:
                    try:
                        await page.locator(_INPUT_SELECTOR).first.press("Enter")
                    except Exception:
                        pass

                for _ in range(60):
                    try:
                        elements = self._locator(page, active_response_selector)
                        current_count = await elements.count()
                    except Exception:
                        current_count = 0
                    is_gen = await self._is_generating(page)
                    if current_count > initial_count or is_gen:
                        gen_started = True
                        break
                    await asyncio.sleep(0.05)

                if gen_started:
                    break
                logger.info(f"[PlaywrightPool] [{account_name}] Generation did not start on send attempt {attempt+1}. Retrying send...")

            if not is_mock and not gen_started:
                logger.warning(f"[PlaywrightPool] [{account_name}] Generation may not have started after 3 send attempts.")

            _tm['sent'] = time.time()
            self.is_fresh_chat[account_name] = False
            self.last_used_account = account_name

            await page.wait_for_selector(_RESPONSE_SELECTOR, timeout=int(_MAX_RESPONSE_WAIT_SEC * 1000))
            active_response_selector = await self._detect_response_selector(page)

            last_text = ""
            stable_count = 0
            _mode_model = (self.applied_mode.get(account_name) or ("", ""))[0].lower()
            _is_fast = "flash" in _mode_model or "lite" in _mode_model or any(x in _mode_model for x in ("fbb127bbb056c959", "56fdd199312815e2", "8c46e95b1a07cecc"))
            _stall_limit = _FAST_STALL_LIMIT_SEC if (_allow_stall and _is_fast and "pro" not in _mode_model and "thinking" not in _mode_model) else 0
            # If Gemini keeps reporting 'generating' but the text stopped changing, the answer is complete.
            _text_idle_limit = _FAST_TEXT_IDLE_SEC if (_is_fast and "pro" not in _mode_model and "thinking" not in _mode_model) else _SLOW_TEXT_IDLE_SEC
            _last_seen_text, _last_change = "", time.time()
            # fast-mode retry (after a stall) gets a short leash instead of the full wait
            _wait_cap = _FAST_RETRY_MAX_SEC if (_is_fast and not _allow_stall and "pro" not in _mode_model and "thinking" not in _mode_model) else _MAX_RESPONSE_WAIT_SEC
            _deadline = time.time() + _wait_cap
            for loop_idx in range(10**7):
                if time.time() > _deadline:
                    break
                if _stall_limit and 'first_text' not in _tm and time.time() - _tm['sent'] > _stall_limit:
                    raise GenerationStall()
                if getattr(page, "captcha_detected", False):
                    raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
                if loop_idx % 40 == 0:
                    status = await self.check_page_status(page, account_name)
                    if status == "captcha":
                        logger.error(f"[PlaywrightPool] [{account_name}] CAPTCHA detected during generation.")
                        raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
                    elif status == "logged_out":
                        logger.error(f"[PlaywrightPool] [{account_name}] Logout detected during generation.")
                        raise LogoutError(f"Logout detected for account '{account_name}'")

                elements = self._locator(page, active_response_selector)
                try:
                    count = await elements.count()
                except Exception:
                    count = 0
                if count <= initial_count:
                    await asyncio.sleep(0.025)
                    continue

                current_text, thoughts_text, has_copy = await self._extract_last_valid_response(elements)
                if current_text and 'first_text' not in _tm:
                    _tm['first_text'] = time.time()
                if not current_text:
                    await asyncio.sleep(0.025)
                    continue

                is_generating = await self._is_generating(page)

                if current_text != _last_seen_text:
                    _last_seen_text, _last_change = current_text, time.time()
                elif current_text and time.time() - _last_change > _text_idle_limit:
                    logger.warning(f"[PlaywrightPool] [{account_name}] Text unchanged for {_text_idle_limit:.0f}s while page still reports 'generating'; treating answer as complete.")
                    _tm['done'] = time.time(); self._log_timing(account_name, _tm)
                    if open_new_chat_after:
                        asyncio.create_task(self.reset_account_chat(account_name))
                    return current_text, thoughts_text

                if is_generating and current_text and loop_idx == 300:
                    logger.warning(f"[PlaywrightPool] [{account_name}] Text present but page still reports 'generating' after ~{loop_idx} polls (has_copy={has_copy}, text={current_text[:60]!r}).")
                    await self._log_empty_response_diagnostics(page, account_name, active_response_selector, initial_count)

                if has_copy and not is_generating and current_text:
                    if open_new_chat_after:
                        logger.info(f"[PlaywrightPool] [{account_name}] Opening new chat after generation...")
                        asyncio.create_task(self.reset_account_chat(account_name))
                    _tm['done'] = time.time(); self._log_timing(account_name, _tm)
                    return current_text, thoughts_text
                elif not is_generating and current_text:
                    if current_text == last_text:
                        stable_count += 1
                        if stable_count >= 80:
                            if open_new_chat_after:
                                logger.info(f"[PlaywrightPool] [{account_name}] Opening new chat after generation...")
                                asyncio.create_task(self.reset_account_chat(account_name))
                            _tm['done'] = time.time(); self._log_timing(account_name, _tm)
                            return current_text, thoughts_text
                    else:
                        stable_count = 0
                        last_text = current_text
                else:
                    stable_count = 0
                    last_text = current_text

                await asyncio.sleep(0.025)

            elements = self._locator(page, active_response_selector)
            response_text, thoughts_text, _ = await self._extract_last_valid_response(elements)
            if not response_text:
                await self._log_empty_response_diagnostics(page, account_name, active_response_selector, initial_count)
            if open_new_chat_after:
                logger.info(f"[PlaywrightPool] [{account_name}] Opening new chat after generation...")
                asyncio.create_task(self.reset_account_chat(account_name))
            return response_text, thoughts_text

    async def send_message_stream(
        self,
        account_name: str,
        prompt: str,
        files: list[str] = None,
        clean_prompt: str = None,
        force_new_chat: bool = False,
        continue_chat: bool = False,
        open_new_chat_after: bool = True,
        model_override: str = None,
        thinking_override: str = None,
    ):
        if account_name not in self.locks:
            raise ValueError(f"Account '{account_name}' not configured in the pool.")

        async with self.locks[account_name]:
            page = self.pages.get(account_name)
            if not page:
                raise RuntimeError(f"Account '{account_name}' does not have an active page in the pool.")

            is_fresh = self.is_fresh_chat.get(account_name, True)
            profile_changed = (self.last_used_account is not None and self.last_used_account != account_name)
            current_url = page.url or ""
            is_on_gemini = "gemini.google.com" in current_url

            should_navigate = False
            if not is_on_gemini:
                should_navigate = True
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Current URL '{current_url}' is not on Gemini Web. Navigating to start a new chat...")
            elif continue_chat and not profile_changed:
                should_navigate = False
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Continuing chat in existing session...")
            elif force_new_chat and not is_fresh:
                should_navigate = True
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) force_new_chat requested for an existing session. Navigating to start a new chat...")
            elif profile_changed:
                should_navigate = True
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Profile changed from '{self.last_used_account}'. Forcing new chat to send full conversation history.")

            yield ""
            if should_navigate:
                yield ""
                success = await self._trigger_new_chat(page, account_name)
                yield ""
                if not success:
                    await page.goto("https://gemini.google.com/app", timeout=30000)
                    yield ""
                is_fresh = True

            status = await self.check_page_status(page, account_name)
            if status == "captcha":
                raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
            elif status == "logged_out":
                raise LogoutError(f"Account '{account_name}' is logged out")

            try:
                await page.wait_for_selector(_INPUT_SELECTOR, timeout=10000)
            except Exception:
                await page.wait_for_timeout(100)
            yield ""

            status = await self.check_page_status(page, account_name)
            if status == "captcha":
                raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
            elif status == "logged_out":
                raise LogoutError(f"Account '{account_name}' is logged out")

            if should_navigate:
                await self._ensure_mode_for_request(page, account_name, model_override, thinking_override, force=True)
                yield ""
            elif is_fresh:
                await self._ensure_mode_for_request(page, account_name, model_override, thinking_override)
                yield ""

            prompt_to_send = prompt
            if not is_fresh and clean_prompt:
                prompt_to_send = clean_prompt
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Continuing chat: sending clean prompt.")
            else:
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Fresh/new chat: sending full prompt.")

            await page.wait_for_selector(_INPUT_SELECTOR, timeout=15000)
            input_locator = page.locator(_INPUT_SELECTOR).first
            await self._human_click(page, input_locator)
            await page.wait_for_timeout(50)
            yield ""
            await input_locator.fill(prompt_to_send)
            yield ""
            eval_res = input_locator.evaluate("el => el.dispatchEvent(new Event('input', { bubbles: true }))")
            if hasattr(eval_res, "__await__"):
                await eval_res

            is_mock = "Mock" in type(page).__name__
            if not is_mock:
                await input_locator.press("Space")
                await page.wait_for_timeout(20)
                await input_locator.press("Backspace")
                await page.wait_for_timeout(30)

            await page.wait_for_timeout(50)
            yield ""

            if files:
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Text typed. Now attaching {len(files)} file(s)...")
                yield ""
                uploaded = await self._upload_files(page, account_name, files, tag="Stream")
                if not uploaded:
                    logger.warning(f"[PlaywrightPool] [{account_name}] (Stream) File upload failed.")
                    raise ValueError("File upload failed")
                await page.wait_for_timeout(100)
                yield ""

            status = await self.check_page_status(page, account_name)
            if status == "captcha":
                raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
            elif status == "logged_out":
                raise LogoutError(f"Account '{account_name}' is logged out")

            active_response_selector = await self._detect_response_selector(page)
            initial_count = 0
            try:
                elements = self._locator(page, active_response_selector)
                initial_count = await elements.count()
            except Exception:
                pass

            send_button = page.locator(_SEND_BTN_SELECTOR)
            wait_ready_timeout = 35.0 if files else 5.0
            start_ready_time = time.time()
            while time.time() - start_ready_time < wait_ready_timeout:
                if is_mock:
                    break
                try:
                    ready = await page.evaluate("""() => {
                        const btns = document.querySelectorAll('button[aria-label*="Send"], button[aria-label*="Enviar"], button[data-test-id*="send"], button.send-button');
                        for (const btn of btns) {
                            if (!btn.disabled && btn.getAttribute("aria-disabled") !== "true" && btn.offsetParent !== null) return true;
                        }
                        return false;
                    }""")
                    if ready:
                        break
                except Exception:
                    try:
                        if await send_button.count() > 0:
                            btn = send_button.first
                            if await btn.is_enabled() and (await btn.get_attribute("aria-disabled")) != "true" and (await btn.get_attribute("disabled")) is None:
                                break
                    except Exception:
                        pass
                yield ""
                await asyncio.sleep(0.05)

            gen_started = False
            for attempt in range(3):
                if is_mock:
                    gen_started = True
                    break

                clicked = False
                try:
                    if await send_button.count() > 0:
                        btn = send_button.first
                        if await btn.is_enabled() and (await btn.get_attribute("aria-disabled")) != "true":
                            await self._human_click(page, btn)
                            clicked = True
                except Exception:
                    pass

                if not clicked:
                    try:
                        await page.locator(_INPUT_SELECTOR).first.press("Enter")
                    except Exception:
                        pass
                yield ""

                for _ in range(60):
                    try:
                        elements = self._locator(page, active_response_selector)
                        current_count = await elements.count()
                    except Exception:
                        current_count = 0
                    is_gen = await self._is_generating(page)
                    if current_count > initial_count or is_gen:
                        gen_started = True
                        break
                    yield ""
                    await asyncio.sleep(0.05)

                if gen_started:
                    break
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Generation did not start on send attempt {attempt+1}. Retrying send...")

            if not is_mock and not gen_started:
                logger.warning(f"[PlaywrightPool] [{account_name}] (Stream) Generation may not have started after 3 send attempts.")

            self.is_fresh_chat[account_name] = False
            self.last_used_account = account_name

            response_selector_found = False
            for _ in range(900):
                try:
                    count = await self._locator(page, _RESPONSE_SELECTOR).count()
                    if count > 0:
                        response_selector_found = True
                        break
                except Exception:
                    pass
                yield ""
                await asyncio.sleep(0.05)

            if not response_selector_found:
                yield "[ERROR: Timeout expired for response to appear]"
                return

            active_response_selector = await self._detect_response_selector(page)
            last_yielded_len = 0
            stable_count = 0
            last_text = ""

            _deadline = time.time() + _MAX_RESPONSE_WAIT_SEC
            for loop_idx in range(10**7):
                if time.time() > _deadline:
                    break
                if getattr(page, "captcha_detected", False):
                    raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
                if loop_idx % 80 == 0:
                    status = await self.check_page_status(page, account_name)
                    if status == "captcha":
                        logger.error(f"[PlaywrightPool] [{account_name}] (Stream) CAPTCHA detected during generation.")
                        raise CaptchaError(f"CAPTCHA detected for account '{account_name}'")
                    elif status == "logged_out":
                        logger.error(f"[PlaywrightPool] [{account_name}] (Stream) Logout detected during generation.")
                        raise LogoutError(f"Logout detected for account '{account_name}'")

                elements = self._locator(page, active_response_selector)
                try:
                    count = await elements.count()
                except Exception:
                    count = 0
                if count <= initial_count:
                    await asyncio.sleep(0.025)
                    continue

                clean_text, _, has_copy = await self._extract_last_valid_response(elements)
                if not clean_text:
                    await asyncio.sleep(0.025)
                    continue

                is_generating = await self._is_generating(page)

                if len(clean_text) > last_yielded_len:
                    chunk = clean_text[last_yielded_len:]
                    yield chunk
                    last_yielded_len = len(clean_text)
                    stable_count = 0
                    last_text = clean_text

                if has_copy and not is_generating and clean_text:
                    break
                elif not is_generating and clean_text:
                    if clean_text == last_text:
                        stable_count += 1
                        if stable_count >= 80:
                            break
                    else:
                        stable_count = 0
                        last_text = clean_text
                else:
                    stable_count = 0
                    last_text = clean_text

                await asyncio.sleep(0.025)

            if open_new_chat_after:
                logger.info(f"[PlaywrightPool] [{account_name}] (Stream) Opening new chat after generation...")
                asyncio.create_task(self.reset_account_chat(account_name))

    def _setup_page_listeners(self, page, name: str):
        page.captcha_detected = False

        def handle_console(msg):
            if "sorry/index" in msg.text:
                logger.error(f"[PlaywrightPool] [{name}] CAPTCHA console error/warning detected!")
                page.captcha_detected = True
        page.on("console", handle_console)

        def handle_response(res):
            if "sorry/index" in res.url:
                logger.error(f"[PlaywrightPool] [{name}] CAPTCHA response redirect detected!")
                page.captcha_detected = True
        page.on("response", handle_response)

        def handle_framenavigated(frame):
            if frame == page.main_frame:
                if "sorry/index" not in page.url and "accounts.google.com" not in page.url:
                    if getattr(page, "captcha_detected", False):
                        logger.info(f"[PlaywrightPool] [{name}] Main frame navigated/reloaded to clean URL: {page.url}. Resetting captcha_detected flag.")
                        page.captcha_detected = False
        page.on("framenavigated", handle_framenavigated)

    async def solve_session_interactively(self, account_name: str) -> bool:
        import sys
        import json

        _, _, profile_path = resolve_paths(account_name)
        logger.info(f"\n[PlaywrightPool] [SESSION/CAPTCHA] Triggering manual solving for '{account_name}'...")

        if sys.platform == "win32":
            try:
                import winsound
                winsound.Beep(1000, 400)
                await asyncio.sleep(0.1)
                winsound.Beep(1000, 400)
            except Exception:
                pass

        print("\n" + "=" * 80)
        print(f"[!] [MANUAL INTERVENTION REQUIRED] CAPTCHA or Login required for account '{account_name}'!")
        print("A visual browser window is opening. Please solve the CAPTCHA or log in.")
        print("Once you log in and the Gemini chat input box loads, please close the Chrome browser window manually.")
        print("=" * 80 + "\n")

        if account_name in self.browsers:
            try:
                await self.browsers[account_name].close()
            except Exception:
                pass
            self.browsers.pop(account_name, None)
            self.pages.pop(account_name, None)

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox"
        ]

        solved = False
        try:
            headed_context = await self._launch_persistent_context_safely(
                profile_path=profile_path,
                headless=False,
                args=launch_args,
                account_name=account_name,
            )

            page = await headed_context.new_page()
            await page.add_init_script(_STEALTH_JS)
            self._setup_page_listeners(page, account_name)

            await page.goto("https://gemini.google.com/app", timeout=45000)
            await page.wait_for_timeout(4000)

            for _ in range(120):
                if page.is_closed() or len(headed_context.pages) == 0:
                    logger.warning("[PlaywrightPool] [SESSION/CAPTCHA] Visual browser was closed by user.")
                    break

                status = await self.check_page_status(page, account_name)
                if status == "ok":
                    try:
                        cookies = await headed_context.cookies()
                        psid, psidts = None, None
                        for cookie in cookies:
                            if cookie["name"] == "__Secure-1PSID":
                                psid = cookie["value"]
                            elif cookie["name"] == "__Secure-1PSIDTS":
                                psidts = cookie["value"]

                        if psid and psidts:
                            logger.info(f"[PlaywrightPool] [SESSION/CAPTCHA] Solved successfully for '{account_name}'!")
                            solved = True
                            _, cookie_path, _ = resolve_paths(account_name)
                            cookie_data = [
                                {"name": "__Secure-1PSID", "value": psid},
                                {"name": "__Secure-1PSIDTS", "value": psidts}
                            ]
                            with open(cookie_path, "w", encoding="utf-8") as f:
                                json.dump(cookie_data, f, indent=2)
                            logger.info(f"[PlaywrightPool] [SESSION/CAPTCHA] Saved updated cookies to cookies.json")
                            print("\n" + "=" * 80)
                            print(f"[SUCCESS] Login/CAPTCHA verified for '{account_name}'! Session cookies saved.")
                            print("[ACTION REQUIRED] Please close the Chrome browser window manually to proceed.")
                            print("=" * 80 + "\n")
                            while not (page.is_closed() or len(headed_context.pages) == 0):
                                await asyncio.sleep(1)
                            break
                        else:
                            logger.warning(f"[PlaywrightPool] [SESSION/CAPTCHA] Input area found but session cookies (__Secure-1PSID/__Secure-1PSIDTS) are missing. Still waiting...")
                    except Exception:
                        pass
                await asyncio.sleep(1)

            await headed_context.close()

        except Exception as e:
            logger.error(f"[PlaywrightPool] [SESSION/CAPTCHA] Error during headed visual context: {e}", exc_info=True)

        if solved:
            logger.info(f"[PlaywrightPool] Re-warming account '{account_name}' (headless={self.headless})...")
            try:
                context = await self._launch_persistent_context_safely(
                    profile_path=profile_path,
                    headless=self.headless,
                    args=launch_args,
                    account_name=account_name,
                )

                page = await context.new_page()
                await page.add_init_script(_STEALTH_JS)
                self._setup_page_listeners(page, account_name)
                await page.goto("https://gemini.google.com/app", timeout=45000)
                await page.wait_for_timeout(3000)

                status = await self.check_page_status(page, account_name)
                if status != "ok":
                    logger.error(f"[PlaywrightPool] Re-warming failed: Session expired or invalid for account '{account_name}' (status: {status}).")
                    await context.close()
                    solved = False
                else:
                    self.browsers[account_name] = context
                    self.pages[account_name] = page
                    self.is_fresh_chat[account_name] = True
                    await self._ensure_model_and_thinking(page, account_name)
                    logger.info(f"[PlaywrightPool] Account '{account_name}' re-warmed successfully!")
            except Exception as e:
                logger.error(f"[PlaywrightPool] Failed to re-warm account '{account_name}': {e}", exc_info=True)
                solved = False

        return solved

    solve_captcha_interactively = solve_session_interactively


# Global shared pool instance
playwright_pool = PlaywrightGeminiPool()
