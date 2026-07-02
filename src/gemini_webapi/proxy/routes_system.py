import os
import base64
import tempfile
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse

from gemini_webapi.proxy.logging import logger


async def handle_gemini_content(request: Request):
    """
    Handles requests in Google Gemini API native format (including inlineData for images).
    """
    logger.info("New native Gemini content request received.")
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid request body."})

    contents = data.get("contents", [])
    if not contents or not isinstance(contents, list):
        return JSONResponse(status_code=400, content={"error": "The 'contents' field is missing or malformed."})

    system_prompt = ""
    if "systemInstruction" in data and isinstance(data["systemInstruction"], dict):
        sys_parts = data["systemInstruction"].get("parts", [])
        for p in sys_parts:
            if isinstance(p, dict) and "text" in p:
                system_prompt += str(p["text"]) + "\n"

    conversation_turns = []
    clean_prompt = ""
    temp_files = []

    # Import dynamically from app to avoid circular dependencies
    from gemini_webapi.proxy.app import (
        load_all_accounts,
        execute_gemini_request_with_retry,
        get_active_model,
        get_active_profile,
        _normalize_media_extension,
    )

    try:
        # Iterate over all parts to extract text and inline media
        for content_item in contents:
            if not isinstance(content_item, dict):
                continue
            role = content_item.get("role", "user")
            role_label = "User" if role.lower() in ("user", "human") else "Assistant"
            parts = content_item.get("parts", [])
            text_parts = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "text" in part:
                    text_parts.append(part["text"])
                elif "inlineData" in part:
                    inline_data = part["inlineData"]
                    mime_type = inline_data.get("mimeType", "image/png")
                    base64_data = inline_data.get("data", "")

                    if base64_data:
                        ext = _normalize_media_extension(mime_type)
                        file_bytes = base64.b64decode(base64_data)
                        source_ref = f"inlineData:{mime_type} [{len(file_bytes)} bytes decoded]"

                        # Compress image using Pillow to speed up upload
                        original_size = len(file_bytes)
                        try:
                            from PIL import Image
                            import io

                            img = Image.open(io.BytesIO(file_bytes))
                            if img.mode in ("RGBA", "P"):
                                img = img.convert("RGB")

                            max_size = 1280
                            if img.width > max_size or img.height > max_size:
                                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

                            output_buf = io.BytesIO()
                            img.save(output_buf, format="JPEG", quality=75, optimize=True)
                            file_bytes = output_buf.getvalue()
                            ext = ".jpg"
                            logger.info(f"Compressed native image from {original_size} bytes to {len(file_bytes)} bytes for upload optimization.")
                        except Exception as img_err:
                            logger.warning(f"Failed to compress native image: {img_err}. Using original image.")

                        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                            temp_file.write(file_bytes)
                            temp_files.append(temp_file.name)
                            logger.info(f"Media file saved to temp: source='{source_ref}' -> temp_path='{temp_file.name}'")

            if text_parts:
                combined_text = " ".join(text_parts).strip()
                if combined_text:
                    conversation_turns.append(f"{role_label}: {combined_text}")
                    if role.lower() in ("user", "human"):
                        clean_prompt = combined_text

        prompt_text = ""
        system_prompt = system_prompt.strip()
        if system_prompt:
            prompt_text += f"[SYSTEM INSTRUCTION]\n{system_prompt}\n[END SYSTEM INSTRUCTION]\n\n"

        if len(conversation_turns) > 1:
            history_turns = conversation_turns[:-1]
            current_turn = conversation_turns[-1]
            prompt_text += "[CONVERSATION HISTORY]\n"
            prompt_text += "\n".join(history_turns)
            prompt_text += "\n[END CONVERSATION HISTORY]\n\n"
            prompt_text += f"[NEW REQUEST]\n{current_turn}"
        elif conversation_turns:
            prompt_text += f"[NEW REQUEST]\n{conversation_turns[0]}"

        if not prompt_text and not temp_files:
            return JSONResponse(status_code=400, content={"error": "No text prompt or media file was sent."})

    except Exception as e:
        for temp_path in temp_files:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        return JSONResponse(status_code=400, content={"error": f"Multimodal content parsing error: {str(e)}"})

    if not load_all_accounts():
        return JSONResponse(status_code=500, content={"error": "No account or authentication cookie configured in profiles/ folder."})

    req_model = data.get("model")
    model_to_use = get_active_model()

    request_thinking = None
    thinking_config = data.get("generationConfig", {}).get("thinkingConfig", {})
    if thinking_config:
        budget = thinking_config.get("thinkingBudget", 0)
        if budget > 0:
            request_thinking = "Extended"
        else:
            request_thinking = "Standard"
    elif req_model and "thinking" in req_model.lower():
        request_thinking = "Extended"

    try:
        response_text = ""
        pool_success = False

        try:
            from playwright_client import playwright_pool, get_all_accounts, CaptchaError, LogoutError

            all_accounts = get_all_accounts()
            active_accounts = []
            for acc in all_accounts:
                name = acc["name"]
                page = playwright_pool.pages.get(name)
                if page:
                    status = await playwright_pool.check_page_status(page, name)
                    if status == "ok":
                        active_accounts.append(name)
                    else:
                        logger.warning(f"[PlaywrightPool] Account '{name}' in native Gemini has status '{status}'. Skipping for now.")

            if not active_accounts and all_accounts:
                account_to_solve = all_accounts[0]["name"]
                logger.warning(f"All Playwright accounts in native Gemini are blocked or logged out. Triggering headed solver for '{account_to_solve}'...")
                solved = await playwright_pool.solve_session_interactively(account_to_solve)
                if solved:
                    active_accounts.append(account_to_solve)

            available_accounts = active_accounts
            active_prof = get_active_profile()
            if active_prof and active_prof in available_accounts:
                playwright_pool.active_account_name = active_prof

            if playwright_pool.active_account_name and playwright_pool.active_account_name in available_accounts:
                available_accounts = [playwright_pool.active_account_name] + [a for a in available_accounts if a != playwright_pool.active_account_name]
            elif available_accounts:
                playwright_pool.active_account_name = available_accounts[0]

            force_new_chat = (len(conversation_turns) <= 1)
            if available_accounts:
                errors = []
                for account_to_use in available_accounts:
                    try:
                        logger.info(f"Attempting Playwright request in native Gemini with account: '{account_to_use}'")
                        response_text, _ = await playwright_pool.send_message(
                            account_to_use,
                            prompt_text,
                            temp_files if temp_files else None,
                            clean_prompt=clean_prompt,
                            force_new_chat=force_new_chat
                        )
                        pool_success = True
                        playwright_pool.active_account_name = account_to_use
                        break
                    except (CaptchaError, LogoutError) as ce:
                        logger.warning(f"Playwright account '{account_to_use}' in native Gemini hit block/logout: {ce}. Trying next account...")
                        errors.append(ce)
                        asyncio.create_task(playwright_pool.reset_account_chat(account_to_use))
                    except Exception as e:
                        logger.warning(f"Playwright account '{account_to_use}' in native Gemini failed: {e}. Trying next account...")
                        errors.append(e)
                        asyncio.create_task(playwright_pool.reset_account_chat(account_to_use))

                if not pool_success:
                    account_to_solve = available_accounts[0]
                    logger.warning(f"All active Playwright accounts in native Gemini failed. Triggering headed solver for '{account_to_solve}'...")
                    solved = await playwright_pool.solve_session_interactively(account_to_solve)
                    if solved:
                        logger.info(f"Solved! Retrying native Gemini request with '{account_to_solve}'...")
                        try:
                            response_text, _ = await playwright_pool.send_message(
                                account_to_solve,
                                prompt_text,
                                temp_files if temp_files else None,
                                clean_prompt=clean_prompt,
                                force_new_chat=force_new_chat
                            )
                            pool_success = True
                            playwright_pool.active_account_name = account_to_solve
                        except Exception as exc:
                            logger.error(f"Failed again in native Gemini after solving: {exc}")
                            errors.append(exc)
                            asyncio.create_task(playwright_pool.reset_account_chat(account_to_solve))
            else:
                logger.warning("No pre-warmed account available in Playwright. Using fallback to Wrapper...")

        except Exception as pool_err:
            logger.warning(f"Failed to use Playwright Pool in native Gemini ({pool_err}). Using API Wrapper as fallback...")

        if not pool_success:
            try:
                response_text, _ = await execute_gemini_request_with_retry(
                    prompt=prompt_text,
                    files=temp_files if temp_files else None,
                    model=model_to_use,
                    request_thinking_level=request_thinking
                )
            except asyncio.TimeoutError:
                err_msg = "⚠️ Gemini proxy error: The gemini_webapi wrapper timed out."
                logger.error(err_msg)
                return JSONResponse(status_code=200, content={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": err_msg}],
                                "role": "model"
                            },
                            "finishReason": "STOP",
                            "index": 0
                        }
                    ]
                })
            except Exception as e:
                err_msg = f"⚠️ Gemini proxy error: Communication error in wrapper: {str(e)}"
                logger.error(err_msg)
                return JSONResponse(status_code=200, content={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": err_msg}],
                                "role": "model"
                            },
                            "finishReason": "STOP",
                            "index": 0
                        }
                    ]
                })
    finally:
        for temp_path in temp_files:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    return JSONResponse(content={
        "candidates": [
            {
                "content": {
                    "parts": [{"text": response_text}],
                    "role": "model"
                },
                "finishReason": "STOP",
                "index": 0
            }
        ]
    })
