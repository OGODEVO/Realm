"""Typed task payload helpers for AgentNet delegation workflows."""

from __future__ import annotations

import json
from typing import Any, Mapping

from agentnet.utils import new_ulid, utc_now_iso

TASK_ASSIGN = "task.assign"
TASK_PROGRESS = "task.progress"
TASK_RESULT = "task.result"
TASK_BLOCKED = "task.blocked"
TASK_FAILED = "task.failed"
TASK_CANCEL = "task.cancel"

TERMINAL_TASK_TYPES = frozenset({TASK_RESULT, TASK_BLOCKED, TASK_FAILED})


def new_task_id(prefix: str = "task") -> str:
    normalized = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in prefix.strip().lower())
    return f"{normalized or 'task'}_{new_ulid().lower()}"


def decode_task_payload(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            if isinstance(decoded, Mapping):
                payload = decoded
    if not isinstance(payload, Mapping):
        return None
    payload_type = str(payload.get("type") or "").strip().lower()
    if payload_type.startswith("task."):
        return payload
    return None


def task_type(payload: Any) -> str:
    decoded = decode_task_payload(payload)
    if decoded is None:
        return ""
    return str(decoded.get("type") or "").strip().lower()


def task_id_from_payload(payload: Any) -> str:
    decoded = decode_task_payload(payload)
    if decoded is None:
        return ""
    return str(decoded.get("task_id") or "").strip()


def is_terminal_task_payload(payload: Any) -> bool:
    return task_type(payload) in TERMINAL_TASK_TYPES


def build_task_assign(
    *,
    task_id: str,
    text: str,
    coordinator: str | None = None,
    title: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": TASK_ASSIGN,
        "task_id": str(task_id),
        "text": str(text),
        "created_at": utc_now_iso(),
        "metadata": dict(metadata or {}),
    }
    if coordinator:
        payload["coordinator"] = str(coordinator)
    if title:
        payload["title"] = str(title)
    return payload


def build_task_progress(*, task_id: str, text: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": TASK_PROGRESS,
        "task_id": str(task_id),
        "text": str(text),
        "event_at": utc_now_iso(),
        "metadata": dict(metadata or {}),
    }


def build_task_result(
    *,
    task_id: str,
    text: str,
    status: str = "completed",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    terminal_type = TASK_RESULT if status == "completed" else TASK_BLOCKED if status == "blocked" else TASK_FAILED
    return {
        "type": terminal_type,
        "task_id": str(task_id),
        "status": status,
        "text": str(text),
        "finished_at": utc_now_iso(),
        "metadata": dict(metadata or {}),
    }
