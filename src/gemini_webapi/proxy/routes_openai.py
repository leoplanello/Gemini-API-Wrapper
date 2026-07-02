import os
import base64
import tempfile
import time
import json
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from gemini_webapi.proxy.logging import logger


async def handle_openai_chat_completions(request: Request):
    """
    Handles requests in OpenAI Chat Completions API format (sent by third-party apps).
    """
    logger.info("New OpenAI Chat Completions request received.")
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid request body."})

    messages = data.get("messages", [])
    if not messages or not isinstance(messages, list):
        return JSONResponse(status_code=400, content={"error": "The 'messages' field is missing or malformed."})

    system_prompt = ""
    temp_files = []
    conversation_turns = []

    # Import dynamically from app and routes_stt to avoid circular dependencies
    from gemini_webapi.proxy.app import (
        load_all_accounts,
        execute_gemini_request_with_retry,
        execute_gemini_stream_with_retry,
        get_active_model,
        get_active_profile,
        _normalize_media_extension,
    )

    try:
        # Iterate over messages to assemble the structured prompt and files
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if not content:
                continue

            if role == "system":
                system_prompt += str(content) + "\n"
            else:
                role_label = "User" if role == "user" else "Assistant"

                # Filter temporary client error messages in the history
                if role == "assistant" and isinstance(content, str):
                    if any(err_msg in content for err_msg in ["Network error during API request", "Failed to fetch", "wrapper gemini_webapi excedeu"]):
                        continue

                if isinstance(content, str):
                    conversation_turns.append(f"{role_label}: {content}")
                elif isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_type = part.get("type")
                        if part_type == "text":
                            text_parts.append(part.get("text", ""))
                        elif part_type == "image_url":
                            image_url_data = part.get("image_url", {}).get("url", "")
                            file_bytes = None
                            ext = ".png"
                            source_ref = "unknown"

                            if image_url_data.startswith("data:image/"):
                                parts = image_url_data.split(";base64,")
                                if len(parts) == 2:
                                    mime_type = parts[0].replace("data:", "")
                                    base64_data = parts[1]
                                    ext = _normalize_media_extension(mime_type)
                                    file_bytes = base64.b64decode(base64_data)
                                    source_ref = f"data:{mime_type};base64 [{len(file_bytes)} bytes decoded]"
                            elif image_url_data.startswith("http://") or image_url_data.startswith("https://"):
                                source_ref = image_url_data
                                try:
                                    import urllib.request
                                    with urllib.request.urlopen(image_url_data, timeout=5) as url_resp:
                                        file_bytes = url_resp.read()
                                        mime_type = url_resp.info().get_content_type()
                                        ext = _normalize_media_extension(mime_type)
                                except Exception as e:
                                    logger.error(f"Error downloading image from URL '{image_url_data}': {e}")

                            if file_bytes:
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
                                    logger.info(f"Compressed image from {original_size} bytes to {len(file_bytes)} bytes for upload optimization.")
                                except Exception as img_err:
                                    logger.warning(f"Failed to compress image: {img_err}. Using original image.")

                                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                                    temp_file.write(file_bytes)
                                    temp_files.append(temp_file.name)
                                    logger.info(f"Media file saved to temp: source='{source_ref}' -> temp_path='{temp_file.name}'")  

                    if text_parts:
                        combined_text = " ".join(text_parts).strip()
                        if combined_text:
                            conversation_turns.append(f"{role_label}: {combined_text}")

        # Reconstruct prompt with clear structure markings
        full_prompt = ""
        system_prompt = system_prompt.strip()
        if system_prompt:
            full_prompt += f"[SYSTEM INSTRUCTION]\n{system_prompt}\n[END SYSTEM INSTRUCTION]\n\n"

        if len(conversation_turns) > 1:
            history_turns = conversation_turns[:-1]
            current_turn = conversation_turns[-1]

            full_prompt += "[CONVERSATION HISTORY]\n"
            full_prompt += "\n".join(history_turns)
            full_prompt += "\n[END CONVERSATION HISTORY]\n\n"

            full_prompt += f"[NEW REQUEST]\n{current_turn}"
        elif conversation_turns:
            full_prompt += f"[NEW REQUEST]\n{conversation_turns[0]}"

        # Adds special context for image if uploaded
        if temp_files:
            full_prompt += "\n\n[CONTEXT]\n(An image of the user's screen/recording is attached. Analyze this image to identify the interview question or coding problem, and output exactly the response, solution, or code needed to answer it, in accordance with the Role and Profile guidelines above.)"

        if not full_prompt and not temp_files:
            return JSONResponse(status_code=400, content={"error": "No prompt or user media found."})

    except Exception as e:
        for temp_path in temp_files:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        return JSONResponse(status_code=400, content={"error": f"Parsing error in OpenAI request: {str(e)}"})

    if not load_all_accounts():
        return JSONResponse(status_code=500, content={"error": "No account or authentication cookie configured in profiles/ folder."})

    req_model = data.get("model")
    reasoning_effort = data.get("reasoning_effort")

    model_to_use = get_active_model()
    request_thinking = None
    if reasoning_effort:
        request_thinking = str(reasoning_effort)
    elif req_model and "thinking" in req_model.lower():
        request_thinking = "Extended"

    try:
        clean_prompt = ""
        if messages:
            # Find the last user/non-system message content
            for msg in reversed(messages):
                if msg.get("role") != "system" and msg.get("content"):
                    content = msg.get("content")
                    if isinstance(content, str):
                        clean_prompt = content
                    elif isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        clean_prompt = " ".join(parts).strip()
                    break

        force_new_chat = (len(conversation_turns) <= 1)

        use_stream = data.get("stream", False)
        response_text = ""
        thoughts_text = ""
        pool_success = False
        playwright_stream_dispatched = False  # True when we handed off to the SSE generator

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
                        logger.warning(f"[PlaywrightPool] Account '{name}' has status '{status}'. Skipping for now.")

            if not active_accounts and all_accounts:
                account_to_solve = all_accounts[0]["name"]
                logger.warning(f"All Playwright accounts are blocked or logged out. Triggering headed solver for '{account_to_solve}'...")
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

            if available_accounts:
                if use_stream:
                    logger.info("Returning response in Stream mode (SSE) via Playwright...")
                    async def event_generator():
                        errors = []
                        stream_success = False

                        try:
                            initial_chunk = {
                                "id": "chatcmpl-geminiproxy",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_to_use,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"role": "assistant", "content": ""},
                                        "finish_reason": None
                                    }
                                ]
                            }
                            yield f"data: {json.dumps(initial_chunk)}\n\n"
                        except Exception as init_err:
                            logger.error(f"Error yielding initial chunk: {init_err}")

                        for account_to_use in available_accounts:
                            try:
                                logger.info(f"Attempting Playwright stream with account: '{account_to_use}'")
                                async for chunk in playwright_pool.send_message_stream(
                                    account_to_use,
                                    full_prompt,
                                    temp_files if temp_files else None,
                                    clean_prompt=clean_prompt,
                                    force_new_chat=force_new_chat
                                ):
                                    if chunk == "":
                                        yield ": keep-alive\n\n"
                                        continue
                                    chunk_data = {
                                        "id": "chatcmpl-geminiproxy",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model_to_use,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": chunk},
                                                "finish_reason": None
                                            }
                                        ]
                                    }
                                    yield f"data: {json.dumps(chunk_data)}\n\n"
                                stream_success = True
                                playwright_pool.active_account_name = account_to_use
                                break
                            except (CaptchaError, LogoutError) as ce:
                                logger.warning(f"Playwright account '{account_to_use}' hit block/logout: {ce}. Trying next account...")
                                errors.append(ce)
                                asyncio.create_task(playwright_pool.reset_account_chat(account_to_use))
                            except Exception as exc:
                                logger.warning(f"Error in Playwright account '{account_to_use}': {exc}. Trying next account...")
                                errors.append(exc)
                                asyncio.create_task(playwright_pool.reset_account_chat(account_to_use))

                        if not stream_success:
                            account_to_solve = available_accounts[0]
                            logger.warning(f"All active Playwright streams failed. Triggering headed solver for '{account_to_solve}'...")
                            solved = await playwright_pool.solve_session_interactively(account_to_solve)
                            if solved:
                                logger.info(f"Solved! Retrying stream with '{account_to_solve}'...")
                                try:
                                    async for chunk in playwright_pool.send_message_stream(
                                        account_to_solve,
                                        full_prompt,
                                        temp_files if temp_files else None,
                                        clean_prompt=clean_prompt,
                                        force_new_chat=force_new_chat
                                    ):
                                        if chunk == "":
                                            yield ": keep-alive\n\n"
                                            continue
                                        chunk_data = {
                                            "id": "chatcmpl-geminiproxy",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": model_to_use,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {"content": chunk},
                                                    "finish_reason": None
                                                }
                                            ]
                                        }
                                        yield f"data: {json.dumps(chunk_data)}\n\n"
                                    stream_success = True
                                    playwright_pool.active_account_name = account_to_solve
                                except Exception as exc:
                                    logger.error(f"Failed again after solving: {exc}")
                                    errors.append(exc)
                                    asyncio.create_task(playwright_pool.reset_account_chat(account_to_solve))

                        if not stream_success:
                            logger.info("Playwright stream failed. Falling back to Wrapper stream...")
                            try:
                                async for chunk in execute_gemini_stream_with_retry(
                                    full_prompt,
                                    temp_files if temp_files else None,
                                    model=model_to_use,
                                    request_thinking_level=request_thinking
                                ):
                                    if chunk:
                                        chunk_data = {
                                            "id": "chatcmpl-geminiproxy",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": model_to_use,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {"content": chunk},
                                                    "finish_reason": None
                                                }
                                            ]
                                        }
                                        yield f"data: {json.dumps(chunk_data)}\n\n"
                                stream_success = True
                            except Exception as wrapper_exc:
                                logger.error(f"Wrapper stream fallback failed: {wrapper_exc}")
                                errors.append(wrapper_exc)

                        if not stream_success:
                            last_err = errors[-1] if errors else "No pre-warmed accounts available."
                            err_chunk = {
                                "id": "chatcmpl-geminiproxy",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_to_use,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": f"⚠️ Playwright pool error: {last_err}"},
                                        "finish_reason": "stop"
                                    }
                                ]
                            }
                            yield f"data: {json.dumps(err_chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                        else:
                            final_chunk = {
                                "id": "chatcmpl-geminiproxy",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_to_use,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": "stop"
                                    }
                                ]
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n"
                            yield "data: [DONE]\n\n"

                    async def event_generator_wrapper():
                        try:
                            async for item in event_generator():
                                yield item
                        finally:
                            for temp_path in temp_files:
                                if os.path.exists(temp_path):
                                    try:
                                        os.remove(temp_path)
                                    except Exception:
                                        pass
                    playwright_stream_dispatched = True
                    headers = {
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no"
                    }
                    return StreamingResponse(event_generator_wrapper(), media_type="text/event-stream", headers=headers)
                else:
                    errors = []
                    for account_to_use in available_accounts:
                        try:
                            logger.info(f"Attempting Playwright request with account: '{account_to_use}'")
                            response_text, thoughts_text = await playwright_pool.send_message(
                                account_to_use,
                                full_prompt,
                                temp_files if temp_files else None,
                                clean_prompt=clean_prompt,
                                force_new_chat=force_new_chat
                            )
                            pool_success = True
                            playwright_pool.active_account_name = account_to_use
                            break
                        except (CaptchaError, LogoutError) as ce:
                            logger.warning(f"Playwright account '{account_to_use}' hit block/logout: {ce}. Trying next account...")
                            errors.append(ce)
                            asyncio.create_task(playwright_pool.reset_account_chat(account_to_use))
                        except Exception as e:
                            logger.warning(f"Playwright account '{account_to_use}' failed: {e}. Trying next account...")
                            errors.append(e)
                            asyncio.create_task(playwright_pool.reset_account_chat(account_to_use))

                    if not pool_success:
                        account_to_solve = available_accounts[0]
                        logger.warning(f"All active Playwright accounts failed. Triggering headed solver for '{account_to_solve}'...")
                        solved = await playwright_pool.solve_session_interactively(account_to_solve)
                        if solved:
                            logger.info(f"Solved! Retrying request with '{account_to_solve}'...")
                            try:
                                response_text, thoughts_text = await playwright_pool.send_message(
                                    account_to_solve,
                                    full_prompt,
                                    temp_files if temp_files else None,
                                    clean_prompt=clean_prompt,
                                    force_new_chat=force_new_chat
                                )
                                pool_success = True
                                playwright_pool.active_account_name = account_to_solve
                            except Exception as exc:
                                logger.error(f"Failed again after solving: {exc}")
                                errors.append(exc)
                                asyncio.create_task(playwright_pool.reset_account_chat(account_to_solve))
            else:
                logger.warning("No pre-warmed account available in Playwright. Using fallback to Wrapper...")

        except Exception as pool_err:
            logger.warning(f"Failed to use Playwright Pool ({pool_err}). Using API Wrapper as fallback...")

        if not pool_success:
            if use_stream:
                logger.info("No Playwright accounts available. Streaming via wrapper (execute_gemini_stream_with_retry)...")
                async def wrapper_event_generator():
                    try:
                        initial_chunk = {
                            "id": "chatcmpl-geminiproxy",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_to_use,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(initial_chunk)}\n\n"
                        async for chunk in execute_gemini_stream_with_retry(
                            full_prompt,
                            temp_files if temp_files else None,
                            model=model_to_use,
                            request_thinking_level=request_thinking
                        ):
                            if chunk:
                                chunk_data = {
                                    "id": "chatcmpl-geminiproxy",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model_to_use,
                                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
                                }
                                yield f"data: {json.dumps(chunk_data)}\n\n"
                        final_chunk = {
                            "id": "chatcmpl-geminiproxy",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_to_use,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                        }
                        yield f"data: {json.dumps(final_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                    finally:
                        for temp_path in temp_files:
                            if os.path.exists(temp_path):
                                try:
                                    os.remove(temp_path)
                                except Exception:
                                    pass
                headers = {
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"
                }
                return StreamingResponse(wrapper_event_generator(), media_type="text/event-stream", headers=headers)

            try:
                response_text, thoughts_text = await execute_gemini_request_with_retry(
                    prompt=full_prompt,
                    files=temp_files if temp_files else None,
                    model=model_to_use,
                    request_thinking_level=request_thinking
                )
            except asyncio.TimeoutError:
                err_msg = "⚠️ Gemini proxy error: The gemini_webapi wrapper timed out."
                logger.error(err_msg)
                return JSONResponse(status_code=200, content={
                    "id": f"chatcmpl-geminiproxy-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_to_use,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": err_msg},
                        "finish_reason": "stop"
                    }]
                })
            except Exception as e:
                err_msg = f"⚠️ Gemini proxy error: Communication error in wrapper: {str(e)}"
                logger.error(err_msg)
                return JSONResponse(status_code=200, content={
                    "id": f"chatcmpl-geminiproxy-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_to_use,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": err_msg},
                        "finish_reason": "stop"
                    }]
                })
    finally:
        if not playwright_stream_dispatched:
            for temp_path in temp_files:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

    message_payload = {
        "role": "assistant",
        "content": response_text
    }
    if thoughts_text:
        message_payload["reasoning_content"] = thoughts_text

    return JSONResponse(content={
        "id": "chatcmpl-geminiproxy",
        "object": "chat.completion",
        "created": 1677652288,
        "model": model_to_use,
        "choices": [
          {
            "index": 0,
            "message": message_payload,
            "finish_reason": "stop"
          }
        ]
    })
