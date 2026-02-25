from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TOOLS_MODULES = (
    "tools.nba_tools",
    "tools.search_tools",
)


@dataclass(slots=True)
class ToolSpec:
    name: str
    doc: str
    signature: str
    fn: Callable[..., Any]


class ToolCatalog:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._load_errors: list[str] = []
        self._load_modules()

    @property
    def load_errors(self) -> list[str]:
        return list(self._load_errors)

    def _load_modules(self) -> None:
        for module_name in TOOLS_MODULES:
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001
                self._load_errors.append(f"{module_name}: {exc}")
                continue

            for name, fn in inspect.getmembers(module, inspect.isfunction):
                if name.startswith("_"):
                    continue
                if fn.__module__ != module_name:
                    continue
                try:
                    signature = str(inspect.signature(fn))
                except ValueError:
                    signature = "()"
                doc = (inspect.getdoc(fn) or "").strip().splitlines()[0] if inspect.getdoc(fn) else ""
                self._tools[name] = ToolSpec(name=name, doc=doc, signature=signature, fn=fn)

    def available(self) -> list[str]:
        return sorted(self._tools.keys())

    def describe(self, names: list[str]) -> str:
        rows: list[str] = []
        for name in names:
            spec = self._tools.get(name)
            if not spec:
                rows.append(f"- {name}: unavailable")
                continue
            summary = spec.doc or "No description."
            rows.append(f"- {spec.name}{spec.signature}: {summary}")
        return "\n".join(rows) if rows else "- No tools configured."

    async def run(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "tool": name, "error": "tool_not_found"}
        safe_args = args or {}
        try:
            result = await asyncio.to_thread(spec.fn, **safe_args)
        except TypeError as exc:
            return {"ok": False, "tool": name, "error": f"bad_args: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "tool": name, "error": str(exc)}

        text: str
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, default=str)
        return {"ok": True, "tool": name, "result": text}


def parse_tool_calls(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    calls_raw: list[dict[str, Any]] = []
    if isinstance(payload.get("tool_calls"), list):
        for item in payload["tool_calls"]:
            if isinstance(item, dict):
                calls_raw.append(item)
    elif payload.get("tool_name"):
        calls_raw.append({"name": payload.get("tool_name"), "args": payload.get("tool_args")})

    parsed: list[dict[str, Any]] = []
    for call in calls_raw:
        name = str(call.get("name") or "").strip()
        if not name:
            continue
        args = call.get("args")
        parsed.append({"name": name, "args": args if isinstance(args, dict) else {}})
    return parsed
