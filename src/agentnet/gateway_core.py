"""Shared helpers for human-facing Realm gateways."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RENDER_TEXT_LIMIT = 3900


def env_bool(name: str, default: bool = False) -> bool:
    value = __import__("os").getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def decode_nested_payload(payload: Any) -> Any:
    """Unwrap SDK text payloads that themselves contain JSON event payloads."""
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            stripped = text.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    return payload
                if isinstance(decoded, dict):
                    return decoded
    return payload


def extract_text(payload: Any) -> str:
    payload = decode_nested_payload(payload)
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return f"{error.strip()}: {detail.strip()}"
            return error.strip()
        return json.dumps(payload, ensure_ascii=False, default=str)
    if isinstance(payload, str):
        return payload.strip()
    return json.dumps(payload, ensure_ascii=False, default=str)


def payload_type(payload: Any) -> str:
    decoded = decode_nested_payload(payload)
    if isinstance(decoded, dict):
        return str(decoded.get("type") or "").strip().lower()
    return ""


def progress_text(payload: Any) -> str | None:
    decoded = decode_nested_payload(payload)
    if not isinstance(decoded, dict):
        return None
    if str(decoded.get("type") or "").strip().lower() != "progress":
        return None
    subtype = str(decoded.get("subtype") or "").strip().lower()
    visible = bool(decoded.get("visible_by_default"))
    text = decoded.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if subtype != "text" or not visible:
        return None
    return text.strip()


def stream_text(payload: Any) -> tuple[str, str | None] | None:
    decoded = decode_nested_payload(payload)
    if not isinstance(decoded, dict):
        return None
    event_type = str(decoded.get("type") or "").strip().lower()
    if event_type == "stream_delta":
        delta = decoded.get("delta")
        if isinstance(delta, str) and delta:
            return "delta", delta
    if event_type == "stream_end":
        text = decoded.get("text")
        return "end", text.strip() if isinstance(text, str) and text.strip() else None
    if event_type == "stream_error":
        error = decoded.get("error")
        return "error", error.strip() if isinstance(error, str) and error.strip() else "stream failed"
    return None


def same_text(left: str | None, right: str | None) -> bool:
    return (left or "").strip() == (right or "").strip()


def render_text(
    target: str,
    text: str,
    *,
    thread_id: str | None = None,
    in_progress: bool = False,
    max_chars: int = RENDER_TEXT_LIMIT,
) -> str:
    body = str(text or "").strip() or "(empty)"
    footer_parts: list[str] = []
    if in_progress:
        footer_parts.append("(still working...)")
    if thread_id:
        footer_parts.append(f"Thread: {thread_id}")
    footer = f"\n\n{chr(10).join(footer_parts)}" if footer_parts else ""
    prefix = f"{target}: "
    budget = max_chars - len(prefix) - len(footer)
    if budget < 200:
        budget = 200
    if len(body) > budget:
        body = body[: budget - 1] + "..."
    return f"{prefix}{body}{footer}"


def normalize_target(value: str) -> str:
    target = value.strip()
    if not target:
        raise ValueError("target is required")
    if target.startswith("@") or target.startswith("acct_") or target.startswith("capability:"):
        return target
    return f"@{target}"


def safe_thread_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return label.strip("_")[:48]


def result_text(result: Any) -> str:
    text = getattr(result, "text", None)
    if not text:
        text = extract_text(getattr(result, "data", None))
    return text or ""


@dataclass(slots=True)
class GatewaySession:
    chat_id: int
    target: str | None = None
    thread_id: str | None = None
    parent_message_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GatewaySession":
        return cls(
            chat_id=int(data["chat_id"]),
            target=str(data["target"]) if data.get("target") else None,
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
            parent_message_id=str(data["parent_message_id"]) if data.get("parent_message_id") else None,
        )


@dataclass(slots=True)
class RenderState:
    chat_id: int
    target: str
    thread_id: str
    message_id: int | None = None
    text: str = ""
    seq_text: list[str] = field(default_factory=list)
    last_edit_at: float = 0.0


class SessionStore:
    """Tiny JSON store for chat-to-thread bindings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = asyncio.Lock()
        self._sessions: dict[int, GatewaySession] = {}
        self._thread_to_chat: dict[str, int] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            sessions = raw.get("sessions", {}) if isinstance(raw, dict) else {}
            for key, value in sessions.items():
                if not isinstance(value, dict):
                    continue
                try:
                    session = GatewaySession.from_dict({"chat_id": int(key), **value})
                except (KeyError, TypeError, ValueError):
                    continue
                self._sessions[session.chat_id] = session
                if session.thread_id:
                    self._thread_to_chat[session.thread_id] = session.chat_id

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "sessions": {
                    str(chat_id): {
                        "target": session.target,
                        "thread_id": session.thread_id,
                        "parent_message_id": session.parent_message_id,
                    }
                    for chat_id, session in sorted(self._sessions.items())
                }
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    async def get(self, chat_id: int) -> GatewaySession:
        async with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = GatewaySession(chat_id=chat_id)
                self._sessions[chat_id] = session
            return GatewaySession(**asdict(session))

    async def put(self, session: GatewaySession) -> None:
        async with self._lock:
            old = self._sessions.get(session.chat_id)
            if old and old.thread_id:
                self._thread_to_chat.pop(old.thread_id, None)
            self._sessions[session.chat_id] = GatewaySession(**asdict(session))
            if session.thread_id:
                self._thread_to_chat[session.thread_id] = session.chat_id
        await self.save()

    async def chat_for_thread(self, thread_id: str | None) -> int | None:
        if not thread_id:
            return None
        async with self._lock:
            return self._thread_to_chat.get(thread_id)
