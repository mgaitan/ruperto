"""ASGI entrypoint for environments that import the app from the repository root."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

app = importlib.import_module("ruperto.app").app
