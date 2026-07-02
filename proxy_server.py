#!/usr/bin/env python3
import sys
import uvicorn
from pathlib import Path

# Add src/ to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi import GeminiClient
from gemini_webapi.proxy.app import (
    app,
    execute_gemini_request_with_retry,
    execute_gemini_stream_with_retry,
    load_all_accounts,
    auto_refresh_account_cookies,
    get_active_model,
    get_active_profile,
    get_headless_setting,
    get_thinking_value,
    map_openai_model_to_gemini,
    _normalize_media_extension,
)

from gemini_webapi.proxy.routes_stt import (
    is_audio_silent,
    pcm_to_wav,
    STT_REFUSAL_PATTERNS,
)

if __name__ == "__main__":
    uvicorn.run("proxy_server:app", host="0.0.0.0", port=8000, reload=False)
