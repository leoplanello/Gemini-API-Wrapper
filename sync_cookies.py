#!/usr/bin/env python3
import sys
from pathlib import Path

# Add src/ to path so package is importable when run directly
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi.utils.cookie_sync import main_cli

if __name__ == "__main__":
    main_cli()
