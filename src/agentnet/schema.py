"""Data models for messages and online agent metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class AgentInfo:
    agent_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_seen: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentInfo":
        agent_id = str(data.get("agent_id") or "")
        name = str(data.get("name") or agent_id)
        capabilities = [str(item) for item in (data.get("capabilities") or [])]
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        last_seen = data.get("last_seen")
        return cls(
            agent_id=agent_id,
            name=name,
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
    kind: str = "direct"
    reply_to: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentMessage":
        return cls(
            message_id=str(data.get("message_id") or ""),
            from_agent=str(data.get("from_agent") or ""),
            to_agent=str(data.get("to_agent") or ""),
            payload=data.get("payload"),
            sent_at=str(data.get("sent_at") or ""),
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
        if self.reply_to is not None:
            payload_dict["reply_to"] = self.reply_to
        return payload_dict
