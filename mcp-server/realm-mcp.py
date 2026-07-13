#!/usr/bin/env python3
"""Compatibility stub — canonical path: drivers/mcp/realm-mcp.py"""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent.parent / "drivers" / "mcp" / "realm-mcp.py"
runpy.run_path(str(_TARGET), run_name="__main__")
