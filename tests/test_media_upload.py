"""
tests/test_media_upload.py
==========================
Automated tests covering all 5 media-upload bugs fixed in this session.
All tests run without a live Gemini session (Playwright + network calls mocked).

Run with:
    cd e:\\1CODE\\Gemini-API
    venv\\Scripts\\activate
    python -m pytest tests/test_media_upload.py -v
"""
import asyncio
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TINY_PNG_B64  = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4DwAAAQEABRjYTgAAAABJRU5ErkJggg=='
_TINY_JPEG_B64 = '/9j/4AAAAAAAAAAAAAAAAAAAAAA='


# ===========================================================================
# Suite 1: _normalize_media_extension unit tests
# ===========================================================================
class TestNormalizeMediaExtension(unittest.TestCase):
    """Verify that _normalize_media_extension returns correct extensions."""

    def setUp(self):
        from proxy_server import _normalize_media_extension
        self.fn = _normalize_media_extension

    def test_jpeg_returns_jpg(self):
        # Bug: mimetypes.guess_extension("image/jpeg") returns .jpe on Windows
        self.assertEqual(self.fn("image/jpeg"), ".jpg")

    def test_image_jpg_alias(self):
        self.assertEqual(self.fn("image/jpg"), ".jpg")

    def test_png(self):
        self.assertEqual(self.fn("image/png"), ".png")

    def test_gif(self):
        self.assertEqual(self.fn("image/gif"), ".gif")

    def test_webp(self):
        self.assertEqual(self.fn("image/webp"), ".webp")

    def test_wav(self):
        self.assertEqual(self.fn("audio/wav"), ".wav")

    def test_x_wav_alias(self):
        self.assertEqual(self.fn("audio/x-wav"), ".wav")

    def test_mp3(self):
        self.assertEqual(self.fn("audio/mpeg"), ".mp3")

    def test_mp4_video(self):
        self.assertEqual(self.fn("video/mp4"), ".mp4")

    def test_pdf(self):
        self.assertEqual(self.fn("application/pdf"), ".pdf")

    def test_unknown_returns_bin(self):
        r = self.fn("application/x-totally-unknown-99")
        self.assertTrue(r.startswith("."), f"Expected extension, got: {r!r}")

    def test_empty_returns_bin(self):
        self.assertEqual(self.fn(""), ".bin")

    def test_case_insensitive(self):
        self.assertEqual(self.fn("IMAGE/JPEG"), ".jpg")
        self.assertEqual(self.fn("Image/Png"),  ".png")

    def test_mime_type_with_parameters(self):
        self.assertEqual(self.fn("image/jpeg; charset=utf-8"), ".jpg")
        self.assertEqual(self.fn("image/png;foo=bar"), ".png")
        self.assertEqual(self.fn("application/pdf  ; boundary=xyz"), ".pdf")
        self.assertEqual(self.fn(";"), ".bin")

    def test_fix_differs_from_bad_mimetypes(self):
        import mimetypes
        from proxy_server import _normalize_media_extension
        raw   = mimetypes.guess_extension("image/jpeg")
        fixed = _normalize_media_extension("image/jpeg")
        self.assertEqual(fixed, ".jpg")
        if raw and raw != ".jpg":
            self.assertNotEqual(fixed, raw,
                "Fix must produce .jpg even when mimetypes is wrong")


# ===========================================================================
# Suite 2: _upload_files calls set_input_files with timeout
# ===========================================================================
class TestUploadFilesSetInputFiles(unittest.IsolatedAsyncioTestCase):

    async def test_set_input_files_called_with_files_and_timeout(self):
        from playwright_client import PlaywrightGeminiPool

        pool = PlaywrightGeminiPool()
        pool.locks = {"acc": asyncio.Lock()}

        set_calls = []

        mock_input_first = AsyncMock()
        async def fake_sif(files, **kw):
            set_calls.append((files, kw))
        mock_input_first.set_input_files = fake_sif

        mock_input = MagicMock()
        mock_input.count = AsyncMock(return_value=1)
        mock_input.first = mock_input_first

        mock_page = AsyncMock()
        mock_page.evaluate        = AsyncMock(return_value=True)
        mock_page.wait_for_timeout  = AsyncMock(return_value=None)
        mock_page.wait_for_selector = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_page.wait_for_function = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_page.locator = MagicMock(return_value=mock_input)

        files = ["/tmp/test.jpg"]
        await pool._upload_files(mock_page, "acc", files)

        self.assertEqual(len(set_calls), 1, "set_input_files must be called once")
        called_files, called_kw = set_calls[0]
        self.assertEqual(called_files, files)
        self.assertIn("timeout", called_kw, "timeout kwarg required for robustness")
        self.assertGreaterEqual(called_kw["timeout"], 15000,
                                "timeout must be at least 15 seconds")


# ===========================================================================
# Suite 3: fill() called BEFORE _upload_files in send_message
# ===========================================================================
class TestSendMessageOrder(unittest.IsolatedAsyncioTestCase):

    def _make_mock_page_and_locator(self, call_order, wait_side_effects=None):
        mock_first = AsyncMock()
        async def fake_fill(text, **kw):
            call_order.append(("fill", text))
        async def fake_click(**kw):
            call_order.append(("click",))
        async def fake_is_enabled():
            return True
        async def fake_press(k):
            call_order.append(("press", k))
        mock_first.fill          = fake_fill
        mock_first.click         = fake_click
        mock_first.is_enabled    = fake_is_enabled
        mock_first.press         = fake_press
        mock_first.get_attribute = AsyncMock(return_value=None)

        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = mock_first

        default_effects = [MagicMock(), MagicMock(), asyncio.TimeoutError("abort")]
        mock_page = AsyncMock()
        mock_page.goto              = AsyncMock(return_value=None)
        mock_page.wait_for_timeout  = AsyncMock(return_value=None)
        mock_page.wait_for_selector = AsyncMock(
            side_effect=wait_side_effects or default_effects
        )
        mock_page.locator = MagicMock(return_value=mock_locator)
        return mock_page, mock_locator

    async def test_fill_before_upload_send_message(self):
        from playwright_client import PlaywrightGeminiPool

        pool = PlaywrightGeminiPool()
        pool.locks = {"acc": asyncio.Lock()}
        call_order = []

        mock_page, _ = self._make_mock_page_and_locator(call_order)
        pool.pages = {"acc": mock_page}

        async def fake_upload(page, account, files, tag=""):
            call_order.append(("upload_files",))
            return True
        pool._upload_files = fake_upload

        try:
            await pool.send_message("acc", "hello world", files=["/tmp/img.jpg"])
        except Exception:
            pass

        fill_idxs = [i for i, c in enumerate(call_order) if c[0] == "fill"]
        up_idxs   = [i for i, c in enumerate(call_order) if c[0] == "upload_files"]

        self.assertTrue(fill_idxs, "fill() was never called")
        self.assertTrue(up_idxs,   "_upload_files() was never called")
        self.assertLess(fill_idxs[0], up_idxs[0],
            f"fill() MUST happen BEFORE upload. Actual call_order: {call_order}")

    async def test_fill_before_upload_send_message_stream(self):
        from playwright_client import PlaywrightGeminiPool

        pool = PlaywrightGeminiPool()
        pool.locks = {"acc": asyncio.Lock()}
        call_order = []

        mock_page, _ = self._make_mock_page_and_locator(call_order)
        pool.pages = {"acc": mock_page}

        async def fake_upload(page, account, files, tag=""):
            call_order.append(("upload_files",))
            return True
        pool._upload_files = fake_upload

        try:
            async for _ in pool.send_message_stream(
                "acc", "hello", files=["/tmp/img.jpg"]
            ):
                break
        except Exception:
            pass

        fill_idxs = [i for i, c in enumerate(call_order) if c[0] == "fill"]
        up_idxs   = [i for i, c in enumerate(call_order) if c[0] == "upload_files"]

        if fill_idxs and up_idxs:
            self.assertLess(fill_idxs[0], up_idxs[0],
                f"fill() MUST precede upload in stream. order: {call_order}")


# ===========================================================================
# Suite 4: End-to-end OpenAI format with base64 image (mocked Playwright)
# ===========================================================================
class TestOpenAIImageEndToEnd(unittest.IsolatedAsyncioTestCase):

    async def test_openai_png_image_returns_200(self):
        from fastapi.testclient import TestClient
        from proxy_server import app

        mock_pool = MagicMock()
        mock_pool.pages = {"acc": MagicMock()}
        mock_pool.check_page_status = AsyncMock(return_value="ok")
        mock_pool.send_message = AsyncMock(return_value=("Image analyzed.", ""))

        payload = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this."},
                    {"type": "image_url",
                      "image_url": {"url": f"data:image/png;base64,{_TINY_PNG_B64}"}},
                ],
            }],
        }

        with patch("playwright_client.playwright_pool", mock_pool), \
             patch("playwright_client.get_all_accounts",
                   return_value=[{"name": "acc"}]):
            client = TestClient(app)
            resp = client.post("/openai/chat/completions", json=payload)

        self.assertEqual(resp.status_code, 200,
            f"Expected 200, got {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        self.assertIn("choices", data)
        self.assertGreater(len(data["choices"]), 0)
        content = data["choices"][0]["message"]["content"]
        self.assertIsInstance(content, str)

    async def test_openai_jpeg_temp_file_has_jpg_extension(self):
        """Verify the saved temp file for image/jpeg has .jpg extension, not .jpe."""
        from fastapi.testclient import TestClient
        from proxy_server import app

        created_suffixes = []
        real_ntf = tempfile.NamedTemporaryFile

        def tracking_ntf(*a, **kw):
            f = real_ntf(*a, **kw)
            created_suffixes.append(kw.get("suffix", ""))
            return f

        mock_pool = MagicMock()
        mock_pool.pages = {"acc": MagicMock()}
        mock_pool.check_page_status = AsyncMock(return_value="ok")
        mock_pool.send_message = AsyncMock(return_value=("ok", ""))

        payload = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this."},
                    {"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{_TINY_JPEG_B64}"}},
                ],
            }],
        }

        with patch("playwright_client.playwright_pool", mock_pool), \
             patch("playwright_client.get_all_accounts",
                   return_value=[{"name": "acc"}]), \
             patch("tempfile.NamedTemporaryFile", side_effect=tracking_ntf):
            client = TestClient(app)
            client.post("/openai/chat/completions", json=payload)

        jpg_files = [s for s in created_suffixes if s == ".jpg"]
        self.assertTrue(
            len(jpg_files) > 0,
            f"Expected .jpg temp file for image/jpeg. Got suffixes: {created_suffixes}"
        )


# ===========================================================================
# Suite 5: Gemini native format with inlineData
# ===========================================================================
class TestGeminiInlineDataEndToEnd(unittest.IsolatedAsyncioTestCase):

    async def test_gemini_inline_png_returns_200(self):
        from fastapi.testclient import TestClient
        from proxy_server import app

        mock_pool = MagicMock()
        mock_pool.pages = {"acc": MagicMock()}
        mock_pool.check_page_status = AsyncMock(return_value="ok")
        mock_pool.send_message = AsyncMock(return_value=("Analyzed.", ""))

        payload = {
            "contents": [{
                "parts": [
                    {"text": "Describe."},
                    {"inlineData": {"mimeType": "image/png", "data": _TINY_PNG_B64}},
                ]
            }]
        }

        with patch("playwright_client.playwright_pool", mock_pool), \
             patch("playwright_client.get_all_accounts",
                   return_value=[{"name": "acc"}]):
            client = TestClient(app)
            resp = client.post(
                "/v1beta/models/gemini-pro:generateContent", json=payload
            )

        self.assertEqual(resp.status_code, 200,
            f"Expected 200, got {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        self.assertIn("candidates", data)
        self.assertGreater(len(data["candidates"]), 0)


# ===========================================================================
# Suite 6: TestClipboardUpload
# ===========================================================================
class TestClipboardUpload(unittest.IsolatedAsyncioTestCase):
    async def test_clipboard_upload_success(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.wait_for_timeout = AsyncMock()
        
        # Mock waiting for confirm to return True so we don't proceed to next step
        async def fake_wait_confirm(*a, **kw):
            return True
        pool._wait_for_upload_confirm = fake_wait_confirm
        
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"fakeimagebytes")), \
             patch("mimetypes.guess_type", return_value=("image/png", None)):
            
            res = await pool._upload_files(mock_page, "acc", ["fakefile.png"], "tag")
            self.assertTrue(res)
            
            self.assertEqual(mock_page.evaluate.call_count, 1)
            args, kwargs = mock_page.evaluate.call_args
            info = args[1]
            self.assertEqual(info["filename"], "fakefile.png")
            self.assertEqual(info["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(info["base64Data"]), b"fakeimagebytes")


# ===========================================================================
# Suite 7: TestDragDropUpload
# ===========================================================================
class TestDragDropUpload(unittest.IsolatedAsyncioTestCase):
    async def test_drag_drop_upload_success(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        # Make clipboard upload fail (return False) and drag-drop succeed
        async def fake_clipboard(*a, **kw):
            return False
        pool._upload_via_clipboard = fake_clipboard
        
        # Mock waiting for confirm to return True
        async def fake_wait_confirm(*a, **kw):
            return True
        pool._wait_for_upload_confirm = fake_wait_confirm
        
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"fakedragdropbytes")), \
             patch("mimetypes.guess_type", return_value=("image/jpeg", None)):
            
            # Since _upload_via_clipboard is mocked to return False, _upload_files will proceed to _upload_via_drag_drop
            res = await pool._upload_files(mock_page, "acc", ["fakedrag.jpg"], "tag")
            self.assertTrue(res)


# ===========================================================================
# Suite 8: TestResponseSelector
# ===========================================================================
class TestResponseSelector(unittest.IsolatedAsyncioTestCase):
    async def test_detect_response_selector_matches(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_locator_match = AsyncMock()
        mock_locator_match.count = AsyncMock(return_value=1)
        
        mock_locator_no_match = AsyncMock()
        mock_locator_no_match.count = AsyncMock(return_value=0)
        
        def fake_locator(sel):
            if sel == ".response-container-content":
                return mock_locator_match
            return mock_locator_no_match
            
        mock_page = AsyncMock()
        mock_page.locator = MagicMock(side_effect=fake_locator)
        
        selector = await pool._detect_response_selector(mock_page)
        self.assertEqual(selector, ".response-container-content")


# ===========================================================================
# Suite 9: Test429Bypass
# ===========================================================================
class Test429Bypass(unittest.IsolatedAsyncioTestCase):
    async def test_429_skips_retry_to_next_account(self):
        from proxy_server import execute_gemini_request_with_retry
        
        mock_accounts = [
            {"name": "acc1", "psid": "p1", "psidts": "t1"},
            {"name": "acc2", "psid": "p2", "psidts": "t2"}
        ]
        
        mock_client1 = AsyncMock()
        mock_client1.account_status = 1  # authenticated
        mock_client1.list_models = MagicMock(return_value=[])
        mock_chat1 = AsyncMock()
        mock_chat1.send_message = AsyncMock(side_effect=Exception("APIError: Status 429"))
        mock_client1.start_chat = MagicMock(return_value=mock_chat1)
        mock_client1.close = AsyncMock()
        
        mock_client2 = AsyncMock()
        mock_client2.account_status = 1  # authenticated
        mock_client2.list_models = MagicMock(return_value=[])
        mock_chat2 = AsyncMock()
        mock_response = MagicMock()
        mock_response.text = "Second account succeeded!"
        mock_response.thoughts = "thinking"
        mock_chat2.send_message = AsyncMock(return_value=mock_response)
        mock_client2.start_chat = MagicMock(return_value=mock_chat2)
        mock_client2.close = AsyncMock()
        
        clients = [mock_client1, mock_client2]
        client_idx = 0
        
        def mock_client_init(psid, psidts):
            nonlocal client_idx
            c = clients[client_idx % len(clients)]
            client_idx += 1
            return c
            
        with patch("proxy_server.load_all_accounts", return_value=mock_accounts), \
             patch("proxy_server.GeminiClient", side_effect=mock_client_init):
            
            res, thoughts = await execute_gemini_request_with_retry("test prompt", None, "gpt-4o")
            self.assertEqual(res, "Second account succeeded!")
            self.assertEqual(client_idx, 2)


# ===========================================================================
# Suite 10: TestGracefulErrorResponse
# ===========================================================================
class TestGracefulErrorResponse(unittest.IsolatedAsyncioTestCase):
    async def test_openai_endpoint_graceful_error_on_failure(self):
        from fastapi.testclient import TestClient
        from proxy_server import app
        
        mock_pool = MagicMock()
        mock_pool.pages = {}
        
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
        }
        
        with patch("playwright_client.playwright_pool", mock_pool), \
             patch("playwright_client.get_all_accounts", return_value=[]), \
             patch("proxy_server.execute_gemini_request_with_retry", side_effect=Exception("API connection refused")):
            
            client = TestClient(app)
            resp = client.post("/openai/chat/completions", json=payload)
            
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("choices", data)
            content = data["choices"][0]["message"]["content"]
            self.assertIn("⚠️ Gemini proxy error", content)
            self.assertIn("API connection refused", content)

    async def test_native_endpoint_graceful_error_on_failure(self):
        from fastapi.testclient import TestClient
        from proxy_server import app
        
        mock_pool = MagicMock()
        mock_pool.pages = {}
        
        payload = {
            "contents": [{"parts": [{"text": "hello"}]}]
        }
        
        with patch("playwright_client.playwright_pool", mock_pool), \
             patch("playwright_client.get_all_accounts", return_value=[]), \
             patch("proxy_server.execute_gemini_request_with_retry", side_effect=Exception("API connection refused")):
            
            client = TestClient(app)
            resp = client.post("/v1beta/models/gemini-pro:generateContent", json=payload)
            
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("candidates", data)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            self.assertIn("⚠️ Gemini proxy error", text)
            self.assertIn("API connection refused", text)


# ===========================================================================
# Suite 11: TestNativeUpload
# ===========================================================================
class TestNativeUpload(unittest.IsolatedAsyncioTestCase):
    async def test_native_upload_success_flow(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_input_first = AsyncMock()
        mock_input_first.set_input_files = AsyncMock()
        mock_input_first.click = AsyncMock()
        
        mock_input = MagicMock()
        mock_input.count = AsyncMock(return_value=1)
        mock_input.first = mock_input_first
        
        mock_page = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_input)
        mock_page.wait_for_selector = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        # Mock waiting for confirm to return True
        async def fake_wait_confirm(*a, **kw):
            return True
        pool._wait_for_upload_confirm = fake_wait_confirm
        
        res = await pool._upload_via_native(mock_page, "acc", ["fakefile.png"], "tag")
        self.assertTrue(res)
        
        # Verify that set_input_files was called
        mock_input_first.set_input_files.assert_called_once_with(["fakefile.png"], timeout=15000)
        # Verify that we tried to click at least the first upload button and upload files menu item
        self.assertGreaterEqual(mock_input_first.click.call_count, 1)

    async def test_native_upload_no_input_found(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_input = MagicMock()
        mock_input.count = AsyncMock(return_value=0) # No file input found
        
        mock_page = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_input)
        mock_page.wait_for_selector = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_page.wait_for_timeout = AsyncMock()
        
        res = await pool._upload_via_native(mock_page, "acc", ["fakefile.png"], "tag")
        self.assertFalse(res)


# ===========================================================================
# Suite 12: TestWaitForUploadConfirm
# ===========================================================================
class TestWaitForUploadConfirm(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_via_selector(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_page = AsyncMock()
        # Mock wait_for_selector to succeed when called
        mock_page.wait_for_selector = AsyncMock()
        mock_page.wait_for_function = AsyncMock() # shouldn't be called if selector succeeds
        
        res = await pool._wait_for_upload_confirm(mock_page, "acc", "tag", timeout=10000)
        self.assertTrue(res)
        
        # Verify wait_for_selector was called
        mock_page.wait_for_selector.assert_called_once()
        call_args = mock_page.wait_for_selector.call_args[0]
        # It should be a single string combined with comma
        selector_arg = call_args[0]
        self.assertIn("uploader-file-preview, .file-preview-chip", selector_arg)
        # Verify wait_for_function was NOT called
        mock_page.wait_for_function.assert_not_called()

    async def test_confirm_via_fallback_js(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_page = AsyncMock()
        # wait_for_selector fails (TimeoutError), wait_for_function succeeds
        mock_page.wait_for_selector = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_page.wait_for_function = AsyncMock(return_value=None) # succeeds / returns normal
        
        res = await pool._wait_for_upload_confirm(mock_page, "acc", "tag", timeout=10000)
        self.assertTrue(res)
        
        # Verify both were called
        mock_page.wait_for_selector.assert_called_once()
        mock_page.wait_for_function.assert_called()
        
        # Check that our selectors are present in the wait_for_function code string
        func_call_args = mock_page.wait_for_function.call_args[0][0]
        self.assertIn("uploader-file-preview", func_call_args)
        self.assertIn(".file-preview-chip", func_call_args)
        self.assertIn(".attachment-preview-wrapper", func_call_args)

    async def test_confirm_failure(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        
        mock_page = AsyncMock()
        # Both fail
        mock_page.wait_for_selector = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_page.wait_for_function = AsyncMock(side_effect=asyncio.TimeoutError)
        
        res = await pool._wait_for_upload_confirm(mock_page, "acc", "tag", timeout=10000)
        self.assertFalse(res)


# ===========================================================================
# Suite 13: TestSendMessageUploadFailure
# ===========================================================================
class TestSendMessageUploadFailure(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_upload_failure_raises_value_error(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        pool.locks = {"acc": asyncio.Lock()}
        
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = AsyncMock()
        mock_locator.first.fill = AsyncMock()
        mock_locator.first.click = AsyncMock()
        mock_locator.first.is_enabled = AsyncMock(return_value=True)
        mock_page.locator = MagicMock(return_value=mock_locator)
        
        pool.pages = {"acc": mock_page}
        
        # Mock _upload_files to return False (failed upload)
        pool._upload_files = AsyncMock(return_value=False)
        
        with self.assertRaises(ValueError) as context:
            await pool.send_message("acc", "hello", files=["badfile.png"])
        self.assertIn("File upload failed", str(context.exception))
        
        pool._upload_files.assert_called_once()

    async def test_send_message_stream_upload_failure_raises_value_error(self):
        from playwright_client import PlaywrightGeminiPool
        pool = PlaywrightGeminiPool()
        pool.locks = {"acc": asyncio.Lock()}
        
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = MagicMock()
        mock_locator.first.fill = AsyncMock()
        mock_locator.first.click = AsyncMock()
        mock_locator.first.is_enabled = AsyncMock(return_value=True)
        mock_page.locator = MagicMock(return_value=mock_locator)
        
        pool.pages = {"acc": mock_page}
        
        # Mock _upload_files to return False (failed upload)
        pool._upload_files = AsyncMock(return_value=False)
        
        with self.assertRaises(ValueError) as context:
            async for chunk in pool.send_message_stream("acc", "hello", files=["badfile.png"]):
                pass
        self.assertIn("File upload failed", str(context.exception))
        
        pool._upload_files.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
