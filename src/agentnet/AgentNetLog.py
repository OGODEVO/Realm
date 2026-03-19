"""Small helpers for clean AgentNet runtime logs."""

from __future__ import annotations

import os
from typing import Iterable


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def agentnet_logs_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return _parse_bool(os.getenv("AGENTNET_LOGS")) or _parse_bool(os.getenv("AGENTNET_PRESENTATION_LOGS"))


def emit_agentnet_log(text: str, *, enabled: bool) -> None:
    if not enabled:
        return
    print(f"[AgentNet] {text}", flush=True)


def short_account_id(account_id: str | None) -> str:
    raw = str(account_id or "").strip()
    if not raw:
        return ""
    if len(raw) <= 18:
        return raw
    return f"{raw[:14]}..."


def format_capabilities(capabilities: Iterable[str] | None) -> str:
    values = [str(item).strip() for item in (capabilities or []) if str(item).strip()]
    return f"[{', '.join(values)}]" if values else "[]"


def format_actor(
    *,
    name: str | None = None,
    username: str | None = None,
    account_id: str | None = None,
    fallback: str | None = None,
) -> str:
    clean_name = str(name or "").strip()
    clean_username = str(username or "").strip().lstrip("@")
    clean_fallback = str(fallback or "").strip()
    if clean_name:
        return clean_name
    if clean_username:
        return f"@{clean_username}"
    if account_id:
        return short_account_id(account_id)
    return clean_fallback or "unknown"


def format_agent_roster_entry(
    *,
    name: str | None = None,
    username: str | None = None,
    capabilities: Iterable[str] | None = None,
) -> str:
    clean_name = str(name or "").strip()
    clean_username = str(username or "").strip().lstrip("@")
    caps = format_capabilities(capabilities)
    if clean_name and clean_username:
        return f"{clean_name} (@{clean_username}) caps={caps}"
    if clean_name:
        return f"{clean_name} caps={caps}"
    if clean_username:
        return f"@{clean_username} caps={caps}"
    return f"unknown caps={caps}"


def format_thread(thread_id: str | None) -> str:
    raw = str(thread_id or "").strip()
    if not raw:
        return ""
    if len(raw) <= 28:
        return raw
    return f"{raw[:24]}..."
