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

_proxy_mutex = None

def acquire_proxy_single_instance_lock() -> bool:
    """Ensure only one instance of Gemini API Wrapper proxy server runs at a time."""
    global _proxy_mutex
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            CreateMutexW = kernel32.CreateMutexW
            CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            CreateMutexW.restype = wintypes.HANDLE
            _proxy_mutex = CreateMutexW(None, False, "Local\\GeminiAPIWrapper_ProxyServer_Mutex")
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                return False
            return True
        except Exception:
            return True
    else:
        try:
            import fcntl
            import os
            lock_path = os.path.join(os.path.expanduser("~"), ".gemini_proxy.lock")
            _proxy_mutex = open(lock_path, "w")
            fcntl.lockf(_proxy_mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            return False
        except Exception:
            return True

if __name__ == "__main__":
    if not acquire_proxy_single_instance_lock():
        print("[Gemini Proxy] Outra instância do Gemini Proxy Server já está em execução ou inicializando. Encerrando esta instância.")
        sys.exit(0)
    import socket

    PORT = 8000
    # Same single port, but answer on IPv6 loopback too: clients resolving "localhost" to ::1 first
    # (python httpx/urllib on Windows) otherwise lose ~2s per connection before falling back to IPv4.
    sockets = []
    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s4.bind(("0.0.0.0", PORT))
    sockets.append(s4)
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s6.bind(("::1", PORT))
        sockets.append(s6)
    except OSError:
        pass  # IPv6 unavailable: IPv4 only, as before

    config = uvicorn.Config("proxy_server:app", host="0.0.0.0", port=PORT, reload=False)
    uvicorn.Server(config).run(sockets=sockets)
