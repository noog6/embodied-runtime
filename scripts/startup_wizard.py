#!/usr/bin/env python3
"""Thin executable entry point for the Startup Wizard."""

from pathlib import Path
import sys

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from embodied_runtime.startup import wizard_main


if __name__ == "__main__":
    raise SystemExit(wizard_main(script_path=Path(__file__)))
