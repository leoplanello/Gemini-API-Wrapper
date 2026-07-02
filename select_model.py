#!/usr/bin/env python3
import sys
import asyncio
from pathlib import Path

# Add src/ to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi.utils.model_selection import run_model_selection

if __name__ == "__main__":
    asyncio.run(run_model_selection())
