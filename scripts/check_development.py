#!/usr/bin/env python3
"""Compatibility alias; all configuration rules live in app.config_check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config_check import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main([*sys.argv[1:], "--profile", "local-development"]))
