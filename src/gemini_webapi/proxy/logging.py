import os
import json
import glob
import logging
import datetime
from logging.handlers import RotatingFileHandler
import sys

LOGS_DIR = "logs"
TRANSACTIONS_DIR = os.path.join(LOGS_DIR, "transactions")
os.makedirs(TRANSACTIONS_DIR, exist_ok=True)

LOG_FORMAT = "%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
logger = logging.getLogger("proxy_server")
logger.setLevel(logging.INFO)

# Console Handler
if not logger.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(console_handler)

    # Handler for Rotating File (max 5MB, keeps up to 3 old files)
    file_handler = RotatingFileHandler(os.path.join(LOGS_DIR, "proxy_server.log"), maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(file_handler)


def mask_cookie(cookie: str) -> str:
    """Hides most of the cookie to avoid exposure in log files."""
    if not cookie:
        return "empty"
    if len(cookie) <= 12:
        return "***"
    return f"{cookie[:6]}...{cookie[-6:]}"


def mask_sensitive_value(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 12:
        return "***"
    return f"{val[:6]}...{val[-6:]}"


def mask_json_payload(data):
    if isinstance(data, dict):
        masked = {}
        for k, v in data.items():
            k_lower = k.lower()
            if any(s in k_lower for s in ("key", "token", "password", "secret", "psid", "cookie", "auth")):
                if isinstance(v, str):
                    masked[k] = mask_sensitive_value(v)
                else:
                    masked[k] = "***"
            elif isinstance(v, str) and (v.startswith("data:") or len(v) > 2000):
                if v.startswith("data:"):
                    prefix = v.split(";base64,")[0] if ";base64," in v else v[:30]
                    masked[k] = f"<{prefix};base64,... [TRUNCATED, TOTAL LENGTH: {len(v)} bytes]>"
                else:
                    masked[k] = v[:200] + f"... [TRUNCATED, TOTAL LENGTH: {len(v)} characters]"
            else:
                masked[k] = mask_json_payload(v)
        return masked
    elif isinstance(data, list):
        return [mask_json_payload(item) for item in data]
    elif isinstance(data, str) and (data.startswith("data:") or len(data) > 2000):
        if data.startswith("data:"):
            prefix = data.split(";base64,")[0] if ";base64," in data else data[:30]
            return f"<{prefix};base64,... [TRUNCATED, TOTAL LENGTH: {len(data)} bytes]>"
        else:
            return data[:200] + f"... [TRUNCATED, TOTAL LENGTH: {len(data)} characters]"
    return data


def prune_old_transactions(max_files: int = 100):
    try:
        files = glob.glob(os.path.join(TRANSACTIONS_DIR, "req_*.json"))
        if len(files) > max_files:
            files.sort()  # Ascending order, oldest first
            to_delete = files[:len(files) - max_files]
            for f in to_delete:
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error cleaning old transaction logs: {e}")


def save_transaction_log(
    method: str,
    path: str,
    client: str,
    req_headers: dict,
    req_body: any,
    status_code: int,
    resp_headers: dict,
    resp_body: any,
    elapsed_time: float,
    success: bool,
    exception: dict | None = None
):
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        clean_path = path.strip("/").replace("/", "_").replace(":", "_") or "root"
        filename = f"req_{timestamp}_{method}_{clean_path}_{status_code}.json"
        filepath = os.path.join(TRANSACTIONS_DIR, filename)

        log_data = {
            "timestamp": datetime.datetime.now().isoformat(),
            "method": method,
            "path": path,
            "client": client,
            "request": {
                "headers": req_headers,
                "body": req_body
            },
            "response": {
                "status_code": status_code,
                "headers": resp_headers,
                "body": resp_body
            },
            "elapsed_time_seconds": elapsed_time,
            "success": success
        }
        if exception:
            log_data["exception"] = exception

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(log_data, f, indent=2, ensure_ascii=False)

        prune_old_transactions()
    except Exception as e:
        logger.error(f"Error saving transaction log file: {e}", exc_info=True)
