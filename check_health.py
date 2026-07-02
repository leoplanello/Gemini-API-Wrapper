#!/usr/bin/env python3
import sys
import asyncio
from pathlib import Path

# Add src/ to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi.utils.health_check import run_health_check

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_health_check())
