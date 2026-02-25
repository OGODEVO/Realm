"""Small helpers for cleaner demo logs in terminal screenshots."""

from __future__ import annotations

from datetime import datetime
from textwrap import fill
from typing import Any

LINE_WIDTH = 96
SEPARATOR = "-" * LINE_WIDTH


def short_id(value: str | None, keep: int = 8) -> str:
    if not value:
        return "-"
    if len(value) <= keep * 2 + 2:
        return value
    return f"{value[:keep]}..{value[-keep:]}"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def print_header(role: str, *, agent_id: str, session_tag: str | None, model: str) -> None:
    print(SEPARATOR)
    print(f"{_ts()} | {role} ONLINE")
    print(f"agent={agent_id} session={short_id(session_tag)} model={model}")
    print(SEPARATOR)


def print_event(
    role: str,
    label: str,
    text: str,
    *,
    turn: int | None = None,
    peer: str | None = None,
    session_tag: str | None = None,
    trace_id: str | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    parent_message_id: str | None = None,
    from_account_id: str | None = None,
    to_account_id: str | None = None,
    status: str | None = None,
    attempt: int | None = None,
    latency_ms: float | None = None,
    handle_ms: float | None = None,
    extra: dict[str, Any] | None = None,
    max_chars: int | None = None,
) -> None:
    print(SEPARATOR)
    left = f"{_ts()} | {role} | {label}"
    if turn is not None:
        left += f" | turn={turn}"
    print(left)

    meta_parts: list[str] = []
    if peer:
        meta_parts.append(f"peer={peer}")
    if session_tag:
        meta_parts.append(f"session={short_id(session_tag)}")
    if trace_id:
        meta_parts.append(f"trace={short_id(trace_id)}")
    if thread_id:
        meta_parts.append(f"thread={short_id(thread_id)}")
    if message_id:
        meta_parts.append(f"msg={short_id(message_id)}")
    if parent_message_id:
        meta_parts.append(f"parent={short_id(parent_message_id)}")
    if from_account_id:
        meta_parts.append(f"from_acct={short_id(from_account_id)}")
    if to_account_id:
        meta_parts.append(f"to_acct={short_id(to_account_id)}")
    if status:
        meta_parts.append(f"status={status}")
    if attempt is not None:
        meta_parts.append(f"attempt={attempt}")
    if latency_ms is not None:
        meta_parts.append(f"latency_ms={latency_ms:.1f}")
    if handle_ms is not None:
        meta_parts.append(f"handle_ms={handle_ms:.1f}")
    if extra:
        for key in sorted(extra):
            value = extra[key]
            if value is None:
                continue
            if isinstance(value, float):
                meta_parts.append(f"{key}={value:.1f}")
            else:
                meta_parts.append(f"{key}={value}")
    if meta_parts:
        print(" ".join(meta_parts))

    normalized = text.strip() or "<empty>"
    if max_chars is not None and max_chars > 0 and len(normalized) > max_chars:
        normalized = normalized[: max_chars - 3].rstrip() + "..."
    print(fill(normalized, width=LINE_WIDTH))
    print(SEPARATOR)


def print_metrics(role: str, label: str, metrics: dict[str, Any], *, turn: int | None = None) -> None:
    print(SEPARATOR)
    left = f"{_ts()} | {role} | {label}"
    if turn is not None:
        left += f" | turn={turn}"
    print(left)
    parts: list[str] = []
    for key in sorted(metrics):
        value = metrics[key]
        if isinstance(value, float):
            parts.append(f"{key}={value:.2f}")
        else:
            parts.append(f"{key}={value}")
    print(" ".join(parts))
    print(SEPARATOR)
