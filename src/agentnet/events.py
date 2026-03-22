"""Helpers for parsing network-emitted system and stream events."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(slots=True)
class StreamStartEvent:
    stream_id: str
    role: str
    content_type: str
    started_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamDeltaEvent:
    stream_id: str
    delta: str
    seq: int
    role: str = "assistant"
    content_type: str = "text/plain"


@dataclass(slots=True)
class StreamEndEvent:
    stream_id: str
    seq: int
    text: str | None = None
    role: str = "assistant"
    content_type: str = "text/plain"
    finished_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamErrorEvent:
    stream_id: str
    error: str
    seq: int = 0
    finished_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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


def is_stream_event(source: AgentMessage | Mapping[str, Any] | Any) -> bool:
    payload = _extract_payload(source)
    if payload is None:
        return False
    return str(payload.get("type") or "").strip().lower() in {
        "stream_start",
        "stream_delta",
        "stream_end",
        "stream_error",
    }


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


def parse_stream_start(source: AgentMessage | Mapping[str, Any] | Any) -> StreamStartEvent | None:
    payload = _extract_payload(source)
    if payload is None or str(payload.get("type") or "").strip().lower() != "stream_start":
        return None
    stream_id = str(payload.get("stream_id") or "").strip()
    if not stream_id:
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    return StreamStartEvent(
        stream_id=stream_id,
        role=str(payload.get("role") or "assistant"),
        content_type=str(payload.get("content_type") or "text/plain"),
        started_at=str(payload.get("started_at")) if payload.get("started_at") is not None else None,
        metadata=dict(metadata),
    )


def parse_stream_delta(source: AgentMessage | Mapping[str, Any] | Any) -> StreamDeltaEvent | None:
    payload = _extract_payload(source)
    if payload is None or str(payload.get("type") or "").strip().lower() != "stream_delta":
        return None
    stream_id = str(payload.get("stream_id") or "").strip()
    if not stream_id:
        return None
    return StreamDeltaEvent(
        stream_id=stream_id,
        delta=str(payload.get("delta") or ""),
        seq=max(0, _as_int(payload.get("seq"))),
        role=str(payload.get("role") or "assistant"),
        content_type=str(payload.get("content_type") or "text/plain"),
    )


def parse_stream_end(source: AgentMessage | Mapping[str, Any] | Any) -> StreamEndEvent | None:
    payload = _extract_payload(source)
    if payload is None or str(payload.get("type") or "").strip().lower() != "stream_end":
        return None
    stream_id = str(payload.get("stream_id") or "").strip()
    if not stream_id:
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    return StreamEndEvent(
        stream_id=stream_id,
        seq=max(0, _as_int(payload.get("seq"))),
        text=str(payload.get("text")) if payload.get("text") is not None else None,
        role=str(payload.get("role") or "assistant"),
        content_type=str(payload.get("content_type") or "text/plain"),
        finished_at=str(payload.get("finished_at")) if payload.get("finished_at") is not None else None,
        metadata=dict(metadata),
    )


def parse_stream_error(source: AgentMessage | Mapping[str, Any] | Any) -> StreamErrorEvent | None:
    payload = _extract_payload(source)
    if payload is None or str(payload.get("type") or "").strip().lower() != "stream_error":
        return None
    stream_id = str(payload.get("stream_id") or "").strip()
    error = str(payload.get("error") or "").strip()
    if not stream_id or not error:
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    return StreamErrorEvent(
        stream_id=stream_id,
        error=error,
        seq=max(0, _as_int(payload.get("seq"))),
        finished_at=str(payload.get("finished_at")) if payload.get("finished_at") is not None else None,
        metadata=dict(metadata),
    )
