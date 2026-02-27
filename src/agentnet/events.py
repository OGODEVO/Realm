"""Helpers for parsing network-emitted system events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agentnet.schema import AgentMessage


def _as_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed


@dataclass(slots=True)
class CompactionRequiredEvent:
    thread_id: str
    status: str
    message_count: int
    byte_count: int
    approx_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    latest_checkpoint_end: int
    requested_at: str | None = None
    reason: str | None = None


def _extract_payload(source: AgentMessage | Mapping[str, Any] | Any) -> Mapping[str, Any] | None:
    if isinstance(source, AgentMessage):
        payload = source.payload
        if isinstance(payload, Mapping):
            return payload
        return None
    if isinstance(source, Mapping):
        # Accept either raw payload dict or full message dict.
        if "payload" in source and isinstance(source.get("payload"), Mapping):
            return source.get("payload")  # type: ignore[return-value]
        return source
    return None


def is_compaction_required(source: AgentMessage | Mapping[str, Any] | Any) -> bool:
    payload = _extract_payload(source)
    if payload is None:
        return False
    return str(payload.get("type") or "").strip().lower() == "compaction_required"


def parse_compaction_required(source: AgentMessage | Mapping[str, Any] | Any) -> CompactionRequiredEvent | None:
    payload = _extract_payload(source)
    if payload is None:
        return None
    if str(payload.get("type") or "").strip().lower() != "compaction_required":
        return None

    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        return None

    return CompactionRequiredEvent(
        thread_id=thread_id,
        status=str(payload.get("status") or "needs_compaction"),
        message_count=max(0, _as_int(payload.get("message_count"))),
        byte_count=max(0, _as_int(payload.get("byte_count"))),
        approx_tokens=max(0, _as_int(payload.get("approx_tokens"))),
        soft_limit_tokens=max(1, _as_int(payload.get("soft_limit_tokens"), default=1)),
        hard_limit_tokens=max(1, _as_int(payload.get("hard_limit_tokens"), default=1)),
        latest_checkpoint_end=max(0, _as_int(payload.get("latest_checkpoint_end"))),
        requested_at=str(payload.get("requested_at")) if payload.get("requested_at") is not None else None,
        reason=str(payload.get("reason")) if payload.get("reason") is not None else None,
    )
