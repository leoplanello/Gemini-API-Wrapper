import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestCaptchaLogin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Patch winsound if on Windows to avoid actually beeping during tests
        if sys.platform == "win32":
            self.beep_patcher = patch("winsound.Beep")
            self.mock_beep = self.beep_patcher.start()

    def tearDown(self):
        if sys.platform == "win32":
            self.beep_patcher.stop()

    async def test_solve_captcha_interactively_success(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        # Setup playwright and chromium mocks
        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_playwright.chromium = mock_chromium
        pool.playwright = mock_playwright
        
        # Setup headed context and page mocks
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.on = MagicMock()
        mock_page.add_init_script = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        mock_headed_context = AsyncMock()
        mock_headed_context.pages = [mock_page]
        mock_headed_context.new_page = AsyncMock(return_value=mock_page)
        
        # Setup headless context and page mocks
        mock_headless_context = AsyncMock()
        mock_headless_page = MagicMock()
        mock_headless_page.on = MagicMock()
        mock_headless_page.add_init_script = AsyncMock()
        mock_headless_page.goto = AsyncMock()
        mock_headless_page.wait_for_timeout = AsyncMock()
        mock_headless_page.url = "https://gemini.google.com/app"
        mock_headless_context.new_page = AsyncMock(return_value=mock_headless_page)
        
        # Configure launch_persistent_context as an AsyncMock that returns these contexts
        mock_launch = AsyncMock()
        mock_launch.side_effect = [
            mock_headed_context,
            mock_headless_context
        ]
        mock_chromium.launch_persistent_context = mock_launch
        
        # Initially, the user is logged out: page is on the landing page (no /app)
        mock_page.url = "https://gemini.google.com/"
        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_page.locator = MagicMock(return_value=mock_locator)
        
        # Cookies are initially empty
        mock_headed_context.cookies = AsyncMock(return_value=[])
        
        # We will change the state of the page after 2 polling iterations:
        # Iteration 0: url="https://gemini.google.com/", cookies=[] -> Should not solve
        # Iteration 1: url="https://accounts.google.com/signin", cookies=[] -> Should not solve
        # Iteration 2: url="https://gemini.google.com/app", cookies=[__Secure-1PSID, __Secure-1PSIDTS] -> Should solve!
        
        states = [
            ("https://gemini.google.com/", []),
            ("https://accounts.google.com/signin", []),
            ("https://gemini.google.com/app", [
                {"name": "__Secure-1PSID", "value": "test-psid"},
                {"name": "__Secure-1PSIDTS", "value": "test-psidts"}
            ])
        ]
        state_idx = 0
        
        def update_state():
            nonlocal state_idx
            if state_idx < len(states):
                url, cookies = states[state_idx]
                mock_page.url = url
                mock_headed_context.cookies.return_value = cookies
                state_idx += 1
        
        # We mock page.wait_for_timeout and asyncio.sleep to control the loop progression
        async def fake_sleep(seconds):
            update_state()
            await asyncio.sleep(0.001)
            
        mock_page.wait_for_timeout = AsyncMock()
        
        # Mock file writing for cookies
        mock_open_mock = unittest.mock.mock_open()
        
        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("builtins.open", mock_open_mock), \
             patch("os.path.abspath", return_value="cookies.json"):
              
            # Run solve_captcha_interactively
            solved = await pool.solve_captcha_interactively("user2")
            
        self.assertTrue(solved)
        # Should have saved the cookies
        mock_open_mock.assert_any_call("cookies.json", "w", encoding="utf-8")
        # Should have re-warmed
        self.assertEqual(pool.browsers["user2"], mock_headless_context)
        self.assertEqual(pool.pages["user2"], mock_headless_page)

    async def test_solve_captcha_interactively_failed_rewarm(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        # Setup playwright and chromium mocks
        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_playwright.chromium = mock_chromium
        pool.playwright = mock_playwright
        
        # Setup headed context and page mocks
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.on = MagicMock()
        mock_page.add_init_script = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        mock_headed_context = AsyncMock()
        mock_headed_context.pages = [mock_page]
        mock_headed_context.new_page = AsyncMock(return_value=mock_page)
        
        # Setup headless context and page mocks
        mock_headless_context = AsyncMock()
        mock_headless_page = MagicMock()
        mock_headless_page.on = MagicMock()
        mock_headless_page.add_init_script = AsyncMock()
        mock_headless_page.goto = AsyncMock()
        mock_headless_page.wait_for_timeout = AsyncMock()
        mock_headless_page.url = "https://accounts.google.com/ServiceLogin"
        mock_headless_context.new_page = AsyncMock(return_value=mock_headless_page)
        
        # Configure launch_persistent_context as an AsyncMock that returns these contexts
        mock_launch = AsyncMock()
        mock_launch.side_effect = [
            mock_headed_context,
            mock_headless_context
        ]
        mock_chromium.launch_persistent_context = mock_launch
        
        # Immediately successful in headed mode
        mock_page.url = "https://gemini.google.com/app"
        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_headed_context.cookies = AsyncMock(return_value=[
            {"name": "__Secure-1PSID", "value": "test-psid"},
            {"name": "__Secure-1PSIDTS", "value": "test-psidts"}
        ])
        
        mock_open_mock = unittest.mock.mock_open()
        
        with patch("builtins.open", mock_open_mock), \
             patch("os.path.abspath", return_value="cookies.json"):
            solved = await pool.solve_captcha_interactively("user2")
            
        # Since re-warming failed, solved should be False
        self.assertFalse(solved)
        mock_headless_context.close.assert_called_once()
        self.assertNotIn("user2", pool.browsers)

    async def test_start_pool_triggers_manual_login(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()

        accounts = [{"name": "test_user", "profile_path": "profiles/test_user"}]
        
        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_playwright.chromium = mock_chromium
        pool.playwright = mock_playwright

        mock_page = MagicMock()
        mock_page.url = "https://gemini.google.com/app"
        mock_page.on = MagicMock()
        mock_page.add_init_script = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_launch = AsyncMock(return_value=mock_context)
        mock_chromium.launch_persistent_context = mock_launch

        status_calls = []
        async def fake_check_page_status(page, name, *args, **kwargs):
            if not status_calls:
                status_calls.append("logged_out")
                return "logged_out"
            return "ok"
            
        pool.check_page_status = fake_check_page_status

        mock_solve = AsyncMock()
        async def side_effect_solve(name):
            pool.browsers[name] = mock_context
            pool.pages[name] = mock_page
            pool.is_fresh_chat[name] = True
            return True
        mock_solve.side_effect = side_effect_solve
        pool.solve_session_interactively = mock_solve
        pool._ensure_model_and_thinking = AsyncMock()

        with patch("playwright_client.get_all_accounts", return_value=accounts), \
             patch("playwright_client.async_playwright") as mock_ap:
            
            mock_ap.return_value.start = AsyncMock(return_value=mock_playwright)
            
            await pool.start_pool(headless=True)

        mock_solve.assert_called_once_with("test_user")
        self.assertEqual(pool.browsers["test_user"], mock_context)
        self.assertEqual(pool.pages["test_user"], mock_page)
        self.assertTrue(pool.is_fresh_chat["test_user"])

    async def test_start_pool_manual_login_fails(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()

        accounts = [{"name": "test_user", "profile_path": "profiles/test_user"}]
        
        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_playwright.chromium = mock_chromium
        pool.playwright = mock_playwright

        mock_page = MagicMock()
        mock_page.url = "https://gemini.google.com/app"
        mock_page.on = MagicMock()
        mock_page.add_init_script = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_launch = AsyncMock(return_value=mock_context)
        mock_chromium.launch_persistent_context = mock_launch

        pool.check_page_status = AsyncMock(return_value="logged_out")
        mock_solve = AsyncMock(return_value=False)
        pool.solve_session_interactively = mock_solve
        pool._ensure_model_and_thinking = AsyncMock()

        with patch("playwright_client.get_all_accounts", return_value=accounts), \
             patch("playwright_client.async_playwright") as mock_ap:
            
            mock_ap.return_value.start = AsyncMock(return_value=mock_playwright)
            
            await pool.start_pool(headless=True)

        mock_solve.assert_called_once_with("test_user")
        self.assertNotIn("test_user", pool.browsers)

if __name__ == "__main__":
    unittest.main()

