"""Small helpers for cleaner demo logs in terminal screenshots."""

from __future__ import annotations

from datetime import datetime
from textwrap import fill

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
    if meta_parts:
        print(" ".join(meta_parts))

    normalized = text.strip() or "<empty>"
    if max_chars is not None and max_chars > 0 and len(normalized) > max_chars:
        normalized = normalized[: max_chars - 3].rstrip() + "..."
    print(fill(normalized, width=LINE_WIDTH))
    print(SEPARATOR)
