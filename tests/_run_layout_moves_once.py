"""One-shot helper: run layout moves then self-skip from normal discovery.

Not a unittest module name pattern test_*.py — only invoked explicitly.
"""
from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    runpy.run_path(str(root / "boot" / "_finish_moves.py"), run_name="__main__")
