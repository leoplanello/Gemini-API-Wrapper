import os
import sys
import time
import struct
import mimetypes
import asyncio
import traceback
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import AuthError
from gemini_webapi.constants import AccountStatus
from gemini_webapi.utils.profile_utils import (
    get_active_profile,
    get_active_profiles,
    get_all_profiles,
    resolve_paths,
    load_cookies_from_json,
    get_active_model,
    get_active_thinking_level,
    SELECTED_THINKING_FILE,
)

from gemini_webapi.proxy.logging import (
    logger,
    mask_cookie,
    mask_sensitive_value,
    mask_json_payload,
    save_transaction_log,
)

# Hardcoded cookies (optional fallback)
SECURE_1PSID = ""
SECURE_1PSIDTS = ""

# Load selected cookies from file
active_prof_name = get_active_profile()
if active_prof_name:
    _, json_cookie_path, _ = resolve_paths(active_prof_name)
    json_psid, json_psidts = load_cookies_from_json(json_cookie_path)
    if json_psid and json_psidts:
        SECURE_1PSID = json_psid
        SECURE_1PSIDTS = json_psidts

HEADLESS_FILE = ".headless"


def get_headless_setting() -> bool:
    """
    Dynamically reads the .headless file to determine whether to run browsers in background.
    """
    if os.path.exists(HEADLESS_FILE):
        try:
            with open(HEADLESS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content != "False"
        except Exception:
            pass
    return True


# Mapping from MIME type to clean file extension
_MIME_TO_EXT = {
    # Images
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
    "image/heic": ".heic",
    "image/heif": ".heif",
    # Audio
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
    # Video
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    # Documents
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
}


def _normalize_media_extension(mime_type: str) -> str:
    if not mime_type:
        return ".bin"
    mime_type = mime_type.split(";")[0].strip()
    if not mime_type:
        return ".bin"
    mime_lower = mime_type.lower()
    if mime_lower in _MIME_TO_EXT:
        return _MIME_TO_EXT[mime_lower]
    guessed = mimetypes.guess_extension(mime_lower)
    if guessed:
        aliases = {".jpe": ".jpg", ".jpeg": ".jpg", ".tif": ".tiff"}
        return aliases.get(guessed, guessed)
    return ".bin"


def get_thinking_value(request_val: str | None = None) -> list[int]:
    """
    Maps the user selection to the expected array value in the Gemini request.
    """
    if request_val:
        request_val_lower = request_val.lower()
        if any(kw in request_val_lower for kw in ["extended", "high", "deep", "medium"]) or request_val == "2":
            return [0]
        elif any(kw in request_val_lower for kw in ["standard", "low", "minimal"]) or request_val == "1":
            return [4]

    if os.path.exists(SELECTED_THINKING_FILE):
        try:
            with open(SELECTED_THINKING_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val == "Extended":
                    return [0]
        except Exception:
            pass
    return [4]


def map_openai_model_to_gemini(model_name: str, available_models: list) -> str | None:
    if not model_name:
        return None

    model_name_lower = model_name.lower()

    # 1. Try direct ID matching
    for m in available_models:
        if model_name_lower == m.model_id.lower():
            return m.model_id

    # 2. Keyword mapping
    target_type = None
    if any(keyword in model_name_lower for keyword in ["thinking", "reasoning", "o1", "o3"]):
        target_type = "thinking"
    elif any(keyword in model_name_lower for keyword in ["pro", "advanced", "ultra"]):
        target_type = "pro"
    elif any(keyword in model_name_lower for keyword in ["flash", "lite", "gpt-4", "gpt-3", "chatcmpl"]):
        target_type = "flash"

    # 3. Scan available models looking for match
    for m in available_models:
        m_name_lower = m.display_name.lower()
        if target_type == "thinking" and "thinking" in m_name_lower:
            return m.model_id
        if target_type == "pro" and "pro" in m_name_lower:
            return m.model_id
        if target_type == "flash" and "flash" in m_name_lower:
            return m.model_id

    # 4. Fallback by partial name matching
    for m in available_models:
        m_name_lower = m.display_name.lower()
        if m_name_lower in model_name_lower or model_name_lower in m_name_lower:
            return m.model_id

    return None


def load_all_accounts() -> list[dict]:
    if "proxy_server" in sys.modules:
        proxy_module = sys.modules["proxy_server"]
        if hasattr(proxy_module, "load_all_accounts"):
            func = getattr(proxy_module, "load_all_accounts")
            if getattr(func, "__module__", None) != "gemini_webapi.proxy.app":
                return func()

    accounts = []
    active_profiles = get_active_profiles()

    for p in active_profiles:
        _, cookie_path, _ = resolve_paths(p)
        psid, psidts = load_cookies_from_json(cookie_path)
        if psid and psidts:
            accounts.append({
                "name": p,
                "psid": psid,
                "psidts": psidts
            })

    if SECURE_1PSID and SECURE_1PSIDTS:
        if not any(acc["psid"] == SECURE_1PSID for acc in accounts):
            accounts.append({
                "name": "hardcoded",
                "psid": SECURE_1PSID,
                "psidts": SECURE_1PSIDTS
            })

    active_prof = get_active_profile()
    if active_prof:
        accounts.sort(key=lambda acc: 0 if acc["name"] == active_prof else 1)

    return accounts



sync_locks = {}


async def auto_refresh_account_cookies(account_name: str) -> bool:
    normalized = account_name.replace("\\", "/").strip()
    if normalized.endswith(".json"):
        normalized = normalized[:-5]
    if "/" in normalized:
        normalized = normalized.split("/")[-1]

    if normalized in ("cookies", "root", "default", "legacy"):
        account_id = "cookies"
    else:
        account_id = normalized

    if account_id not in sync_locks:
        sync_locks[account_id] = asyncio.Lock()

    async with sync_locks[account_id]:
        logger.info(f"[Self-Healing] Starting background cookie update for account '{account_id}'...")
        try:
            from gemini_webapi.utils.cookie_sync import sync_account
            success = await sync_account(account_id, headless=True)
            if success:
                logger.info(f"[Self-Healing] Successfully updated cookies for account '{account_id}'!")
                return True
            else:
                logger.warning(f"[Self-Healing] Failed to update cookies for account '{account_id}'.")
        except Exception as e:
            logger.error(f"[Self-Healing] Error syncing cookies for account '{account_id}': {e}", exc_info=True)
    return False


async def execute_gemini_request_with_retry(
    prompt: str,
    files: list[str] | None,
    model: str,
    request_thinking_level: str | None = None
) -> tuple[str, str]:
    if "proxy_server" in sys.modules:
        proxy_module = sys.modules["proxy_server"]
        if hasattr(proxy_module, "execute_gemini_request_with_retry"):
            func = getattr(proxy_module, "execute_gemini_request_with_retry")
            if getattr(func, "__module__", None) != "gemini_webapi.proxy.app":
                res = func(prompt, files, model, request_thinking_level)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    return await res
                return res

    accounts = load_all_accounts()
    if not accounts:
        logger.error("No authentication cookies configured in accounts.")
        raise RuntimeError("No authentication cookies configured in accounts.")

    logger.info(f"Starting request. Prompt length: {len(prompt)}, Model: {model}, Files count: {len(files) if files else 0}")
    last_exception = None

    for global_attempt in range(1, 4):
        accounts = load_all_accounts()
        logger.info(f"Starting global attempt cycle {global_attempt}/3. Loaded accounts: {len(accounts)}")

        for idx, account in enumerate(accounts, start=1):
            account_name = account["name"]
            masked_psid = mask_cookie(account["psid"])
            logger.info(f"Using account {idx}/{len(accounts)} ({account_name}) - PSID: {masked_psid}...")
            sys.modules[__name__].last_used_account = f"{account_name} (PSID: {masked_psid})"

            # --- Attempt 1: Normal Chat Session ---
            gclient_class = GeminiClient
            if "proxy_server" in sys.modules:
                proxy_module = sys.modules["proxy_server"]
                if hasattr(proxy_module, "GeminiClient"):
                    mock_gclient = getattr(proxy_module, "GeminiClient")
                    if getattr(mock_gclient, "__module__", None) != "gemini_webapi.client":
                        gclient_class = mock_gclient
            client = gclient_class(account["psid"], account["psidts"])
            client.thinking_level = get_thinking_value(request_thinking_level)
            try:
                await client.init(timeout=30)
                if client.account_status == AccountStatus.UNAUTHENTICATED:
                    raise AuthError("Cookies expired or invalid session.")

                resolved_model = model
                available_models = client.list_models()
                if available_models:
                    mapped = map_openai_model_to_gemini(model, available_models)
                    if mapped:
                        resolved_model = mapped
                        logger.info(f"Model successfully mapped: '{model}' -> '{resolved_model}'")
                    else:
                        fallback = get_active_model()
                        mapped_fallback = map_openai_model_to_gemini(fallback, available_models)
                        resolved_model = mapped_fallback or fallback
                        logger.warning(f"Could not map client model '{model}'. Falling back to configured model: '{resolved_model}'")

                chat = client.start_chat(model=resolved_model)
                response = await chat.send_message(prompt, files=files if files else None)

                try:
                    await client.close()
                except Exception:
                    pass

                response_text = (response.text if hasattr(response, 'text') else str(response)) or ""
                thoughts_text = (response.thoughts if hasattr(response, 'thoughts') else "") or ""
                logger.info(f"Success on account {account_name} (Attempt 1)! Response length: {len(response_text)}, Thoughts length: {len(thoughts_text)}")
                return response_text, thoughts_text
            except AuthError as ae:
                logger.warning(f"Authentication failure on account {account_name} (Attempt 1): {ae}. Triggering self-healing...")
                last_exception = ae
                try:
                    await client.close()
                except Exception:
                    pass

                if len(accounts) > 1:
                    logger.info(f"Multiple accounts available ({len(accounts)}). Triggering background self-healing for '{account_name}' and moving to next account...")
                    asyncio.create_task(auto_refresh_account_cookies(account_name))
                    continue
                else:
                    logger.info(f"Only one account available. Waiting for self-healing to complete for '{account_name}'...")
                    refreshed = await auto_refresh_account_cookies(account_name)
                    if refreshed:
                        updated_accounts = load_all_accounts()
                        for acc in updated_accounts:
                            if acc["name"] == account_name:
                                account["psid"] = acc["psid"]
                                account["psidts"] = acc["psidts"]
                                logger.info(f"Account {account_name} updated in memory with new cookies.")
                                break
            except Exception as e:
                logger.warning(f"Failure on account {account_name} (Attempt 1): {e}", exc_info=True)
                last_exception = e
                try:
                    await client.close()
                except Exception:
                    pass

                if "429" in str(e):
                    logger.warning(f"Account {account_name} rate limited (429). Skipping simple retry and moving to next account.")
                    continue

            # --- Attempt 2: Simple Retry (New Clean Session on the same account) ---
            logger.info(f"Executing simple retry (Attempt 2) on account {account_name}...")
            sys.modules[__name__].last_used_account = f"{account_name} (PSID: {masked_psid})"
            gclient_class = GeminiClient
            if "proxy_server" in sys.modules:
                proxy_module = sys.modules["proxy_server"]
                if hasattr(proxy_module, "GeminiClient"):
                    mock_gclient = getattr(proxy_module, "GeminiClient")
                    if getattr(mock_gclient, "__module__", None) != "gemini_webapi.client":
                        gclient_class = mock_gclient
            client = gclient_class(account["psid"], account["psidts"])
            client.thinking_level = get_thinking_value(request_thinking_level)
            try:
                await client.init(timeout=30)
                if client.account_status == AccountStatus.UNAUTHENTICATED:
                    raise AuthError("Cookies expired or invalid session.")

                resolved_model = model
                available_models = client.list_models()
                if available_models:
                    mapped = map_openai_model_to_gemini(model, available_models)
                    if mapped:
                        resolved_model = mapped
                        logger.info(f"Model successfully mapped on retry: '{model}' -> '{resolved_model}'")
                    else:
                        fallback = get_active_model()
                        mapped_fallback = map_openai_model_to_gemini(fallback, available_models)
                        resolved_model = mapped_fallback or fallback
                        logger.warning(f"Could not map client model '{model}' on retry. Falling back to configured model: '{resolved_model}'")

                chat = client.start_chat(model=resolved_model)
                response = await chat.send_message(prompt, files=files if files else None)

                try:
                    await client.close()
                except Exception:
                    pass

                response_text = (response.text if hasattr(response, 'text') else str(response)) or ""
                thoughts_text = (response.thoughts if hasattr(response, 'thoughts') else "") or ""
                logger.info(f"Success on account {account_name} (Attempt 2 - Simple Retry)! Response length: {len(response_text)}, Thoughts length: {len(thoughts_text)}")
                return response_text, thoughts_text
            except AuthError as ae:
                logger.warning(f"Authentication failure on account {account_name} (Attempt 2 - Simple Retry): {ae}. Triggering self-healing...")
                last_exception = ae
                try:
                    await client.close()
                except Exception:
                    pass

                if len(accounts) > 1:
                    logger.info(f"Multiple accounts available ({len(accounts)}). Triggering background self-healing for '{account_name}'...")
                    asyncio.create_task(auto_refresh_account_cookies(account_name))
                else:
                    logger.info(f"Only one account available. Waiting for self-healing to complete for '{account_name}'...")
                    await auto_refresh_account_cookies(account_name)
            except Exception as e:
                logger.warning(f"Failure on account {account_name} (Attempt 2 - Simple Retry): {e}", exc_info=True)
                last_exception = e
                try:
                    await client.close()
                except Exception:
                    pass

    if last_exception:
        logger.error(f"All attempts failed. Final error: {last_exception}")
        raise last_exception
    else:
        logger.error("All attempts failed. No response obtained.")
        raise RuntimeError("Could not obtain response from Gemini after all attempts.")


async def execute_gemini_stream_with_retry(
    prompt: str,
    files: list[str] | None,
    model: str,
    request_thinking_level: str | None = None
):
    accounts = load_all_accounts()
    if not accounts:
        raise AuthError("No accounts found in accounts.json.")

    last_exception = None

    for global_attempt in range(3):
        for account in accounts:
            account_name = account.get("name", "Unnamed Account")
            if not account.get("psid") or not account.get("psidts"):
                continue

            client = GeminiClient(account["psid"], account["psidts"])
            client.thinking_level = get_thinking_value(request_thinking_level)
            sys.modules[__name__].last_used_account = f"{account_name} (PSID: {mask_cookie(account.get('psid', ''))})"

            try:
                await client.init(timeout=30)
                if client.account_status == AccountStatus.UNAUTHENTICATED:
                    raise AuthError("Cookies expired or invalid session.")

                resolved_model = model
                available_models = client.list_models()
                if available_models:
                    mapped = map_openai_model_to_gemini(model, available_models)
                    if mapped:
                        resolved_model = mapped
                    else:
                        fallback = get_active_model()
                        mapped_fallback = map_openai_model_to_gemini(fallback, available_models)
                        resolved_model = mapped_fallback or fallback

                chat = client.start_chat(model=resolved_model)
                stream_succeeded = False
                async for output in chat.send_message_stream(prompt, files=files if files else None):
                    stream_succeeded = True
                    text_delta = getattr(output, "text_delta", "")
                    if text_delta:
                        yield text_delta

                try:
                    await client.close()
                except Exception:
                    pass

                if stream_succeeded:
                    return
            except AuthError as ae:
                logger.warning(f"Auth failure in stream on account {account_name}: {ae}. Triggering self-healing...")
                last_exception = ae
                try:
                    await client.close()
                except Exception:
                    pass
                if len(accounts) > 1:
                    asyncio.create_task(auto_refresh_account_cookies(account_name))
                    continue
                else:
                    await auto_refresh_account_cookies(account_name)
            except Exception as e:
                logger.warning(f"Failure in stream on account {account_name}: {e}", exc_info=True)
                last_exception = e
                try:
                    await client.close()
                except Exception:
                    pass

    if last_exception:
        raise last_exception
    else:
        raise RuntimeError("Could not stream response from Gemini after all attempts.")


app = FastAPI(title="Gemini & Speech Proxy Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    logger.info("==================================================")
    logger.info("Starting proxy server. Pre-warming Playwright pool...")

    accounts = load_all_accounts()
    logger.info(f"Total cookie accounts loaded: {len(accounts)}")
    for idx, acc in enumerate(accounts, start=1):
        masked_psid = mask_cookie(acc["psid"])
        logger.info(f"  Account {idx}: {acc['name']} (PSID: {masked_psid})")

    try:
        from playwright_client import playwright_pool
        await playwright_pool.start_pool(headless=get_headless_setting())
    except Exception as e:
        logger.error(f"Error initializing Playwright pool: {e}", exc_info=True)

    logger.info("==================================================")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("==================================================")
    logger.info("Shutting down server. Stopping Playwright pool...")
    try:
        from playwright_client import playwright_pool
        await playwright_pool.close()
    except Exception as e:
        logger.error(f"Error stopping Playwright pool: {e}", exc_info=True)
    logger.info("==================================================")


@app.post("/{path:path}")
async def proxy(path: str, request: Request):
    """
    Single entry point to intercept all POST requests,
    with detailed logging and error handling.
    """
    method = request.method
    client_host = request.client.host if request.client else "unknown"

    # 1. Capture and mask request headers
    req_headers = {}
    for k, v in request.headers.items():
        if k.lower() in ("authorization", "cookie", "x-api-key", "api-key"):
            req_headers[k] = mask_sensitive_value(v)
        else:
            req_headers[k] = v

    # 2. Capture and mask request body
    body_bytes = b""
    req_body_json = None
    try:
        body_bytes = await request.body()
        async def receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        request._receive = receive

        if body_bytes:
            try:
                req_body_json = json.loads(body_bytes.decode("utf-8"))
                req_body_json = mask_json_payload(req_body_json)
            except Exception:
                req_body_json = body_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        req_body_json = f"<Error reading request body: {str(e)}>"

    start_time = time.time()
    logger.info(f"[{method}] {path} from {client_host} received.")

    response_status = 500
    resp_headers = {}
    resp_body_json = None

    # Lazy import of routes to prevent circular imports
    from gemini_webapi.proxy.routes_stt import handle_speech_recognition
    from gemini_webapi.proxy.routes_openai import handle_openai_chat_completions
    from gemini_webapi.proxy.routes_system import handle_gemini_content

    try:
        if "speech:recognize" in path:
            response = await handle_speech_recognition(request)
        elif "openai/chat/completions" in path or "chat/completions" in path:
            response = await handle_openai_chat_completions(request)
        else:
            response = await handle_gemini_content(request)

        elapsed_time = time.time() - start_time
        response_status = response.status_code

        for k, v in response.headers.items():
            resp_headers[k] = v

        if hasattr(response, "body"):
            try:
                resp_body_json = json.loads(response.body.decode("utf-8"))
                resp_body_json = mask_json_payload(resp_body_json)
            except Exception:
                resp_body_json = response.body.decode("utf-8", errors="replace")

        logger.info(f"[{method}] {path} from {client_host} -> Status: {response_status} in {elapsed_time:.4f}s")

        save_transaction_log(
            method=method,
            path=path,
            client=client_host,
            req_headers=req_headers,
            req_body=req_body_json,
            status_code=response_status,
            resp_headers=resp_headers,
            resp_body=resp_body_json,
            elapsed_time=elapsed_time,
            success=(200 <= response_status < 300)
        )
        return response

    except Exception as e:
        elapsed_time = time.time() - start_time
        exception_info = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc()
        }
        logger.error(f"Error processing [{method}] {path} from {client_host}: {str(e)}\n{traceback.format_exc()}")

        save_transaction_log(
            method=method,
            path=path,
            client=client_host,
            req_headers=req_headers,
            req_body=req_body_json,
            status_code=500,
            resp_headers={},
            resp_body=None,
            elapsed_time=elapsed_time,
            success=False,
            exception=exception_info
        )
        raise e
