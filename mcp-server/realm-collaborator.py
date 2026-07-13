#!/usr/bin/env python3
"""Compatibility stub — canonical path: drivers/mcp/realm-collaborator.py"""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent.parent / "drivers" / "mcp" / "realm-collaborator.py"
runpy.run_path(str(_TARGET), run_name="__main__")
