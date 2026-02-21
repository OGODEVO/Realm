"""Data models for messages and online agent metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class AgentInfo:
    agent_id: str
    name: str
    session_tag: str | None = None
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_seen: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentInfo":
        agent_id = str(data.get("agent_id") or "")
        name = str(data.get("name") or agent_id)
        session_tag = data.get("session_tag")
        capabilities = [str(item) for item in (data.get("capabilities") or [])]
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        last_seen = data.get("last_seen")
        return cls(
            agent_id=agent_id,
            name=name,
            session_tag=str(session_tag) if session_tag else None,
            capabilities=capabilities,
            metadata=metadata,
            last_seen=str(last_seen) if last_seen else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_id": self.agent_id,
            "name": self.name,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }
        if self.session_tag is not None:
            payload["session_tag"] = self.session_tag
        if self.last_seen is not None:
            payload["last_seen"] = self.last_seen
        return payload


@dataclass(slots=True)
class AgentMessage:
    message_id: str
    from_agent: str
    to_agent: str
    payload: Any
    sent_at: str
    from_session_tag: str | None = None
    ttl_ms: int | None = None
    expires_at: str | None = None
    trace_id: str | None = None
    kind: str = "direct"
    reply_to: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentMessage":
        ttl_raw = data.get("ttl_ms")
        ttl_ms: int | None
        try:
            ttl_ms = int(ttl_raw) if ttl_raw is not None else None
        except (TypeError, ValueError):
            ttl_ms = None

        return cls(
            message_id=str(data.get("message_id") or ""),
            from_agent=str(data.get("from_agent") or ""),
            to_agent=str(data.get("to_agent") or ""),
            payload=data.get("payload"),
            sent_at=str(data.get("sent_at") or ""),
            from_session_tag=str(data.get("from_session_tag") or "") or None,
            ttl_ms=ttl_ms,
            expires_at=str(data.get("expires_at") or "") or None,
            trace_id=str(data.get("trace_id") or "") or None,
            kind=str(data.get("kind") or "direct"),
            reply_to=data.get("reply_to"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload_dict = {
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "payload": self.payload,
            "sent_at": self.sent_at,
            "kind": self.kind,
        }
        if self.from_session_tag is not None:
            payload_dict["from_session_tag"] = self.from_session_tag
        if self.ttl_ms is not None:
            payload_dict["ttl_ms"] = self.ttl_ms
        if self.expires_at is not None:
            payload_dict["expires_at"] = self.expires_at
        if self.trace_id is not None:
            payload_dict["trace_id"] = self.trace_id
        if self.reply_to is not None:
            payload_dict["reply_to"] = self.reply_to
        return payload_dict
