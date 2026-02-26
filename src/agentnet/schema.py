"""Data models for messages and online agent metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class AgentInfo:
    agent_id: str
    name: str
    account_id: str | None = None
    username: str | None = None
    session_tag: str | None = None
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_seen: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentInfo":
        agent_id = str(data.get("agent_id") or "")
        name = str(data.get("name") or agent_id)
        account_id = data.get("account_id")
        username = data.get("username")
        session_tag = data.get("session_tag")
        capabilities = [str(item) for item in (data.get("capabilities") or [])]
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        last_seen = data.get("last_seen")
        return cls(
            agent_id=agent_id,
            name=name,
            account_id=str(account_id) if account_id else None,
            username=str(username) if username else None,
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
        if self.account_id is not None:
            payload["account_id"] = self.account_id
        if self.username is not None:
            payload["username"] = self.username
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
    from_account_id: str | None = None
    to_account_id: str | None = None
    from_session_tag: str | None = None
    ttl_ms: int | None = None
    expires_at: str | None = None
    trace_id: str | None = None
    thread_id: str | None = None
    parent_message_id: str | None = None
    kind: str = "direct"
    schema_version: str = "1.0"
    idempotency_key: str | None = None
    reply_to: str | None = None
    auth: dict[str, Any] | None = None

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
            from_account_id=str(data.get("from_account_id") or "") or None,
            to_account_id=str(data.get("to_account_id") or "") or None,
            from_session_tag=str(data.get("from_session_tag") or "") or None,
            ttl_ms=ttl_ms,
            expires_at=str(data.get("expires_at") or "") or None,
            trace_id=str(data.get("trace_id") or "") or None,
            thread_id=str(data.get("thread_id") or "") or None,
            parent_message_id=str(data.get("parent_message_id") or "") or None,
            kind=str(data.get("kind") or "direct"),
            schema_version=str(data.get("schema_version") or "1.0"),
            idempotency_key=str(data.get("idempotency_key") or "") or None,
            reply_to=data.get("reply_to"),
            auth=data.get("auth") if isinstance(data.get("auth"), dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload_dict = {
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "payload": self.payload,
            "sent_at": self.sent_at,
            "kind": self.kind,
            "schema_version": self.schema_version or "1.0",
        }
        if self.from_session_tag is not None:
            payload_dict["from_session_tag"] = self.from_session_tag
        if self.from_account_id is not None:
            payload_dict["from_account_id"] = self.from_account_id
        if self.to_account_id is not None:
            payload_dict["to_account_id"] = self.to_account_id
        if self.ttl_ms is not None:
            payload_dict["ttl_ms"] = self.ttl_ms
        if self.expires_at is not None:
            payload_dict["expires_at"] = self.expires_at
        if self.trace_id is not None:
            payload_dict["trace_id"] = self.trace_id
        if self.thread_id is not None:
            payload_dict["thread_id"] = self.thread_id
        if self.parent_message_id is not None:
            payload_dict["parent_message_id"] = self.parent_message_id
        if self.idempotency_key is not None:
            payload_dict["idempotency_key"] = self.idempotency_key
        if self.reply_to is not None:
            payload_dict["reply_to"] = self.reply_to
        if self.auth is not None:
            payload_dict["auth"] = self.auth
        return payload_dict


@dataclass(slots=True)
class DeliveryReceipt:
    message_id: str
    status: str
    event_at: str
    from_account_id: str | None = None
    from_session_tag: str | None = None
    to_account_id: str | None = None
    trace_id: str | None = None
    thread_id: str | None = None
    parent_message_id: str | None = None
    code: str | None = None
    detail: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeliveryReceipt":
        return cls(
            message_id=str(data.get("message_id") or ""),
            status=str(data.get("status") or ""),
            event_at=str(data.get("event_at") or ""),
            from_account_id=str(data.get("from_account_id") or "") or None,
            from_session_tag=str(data.get("from_session_tag") or "") or None,
            to_account_id=str(data.get("to_account_id") or "") or None,
            trace_id=str(data.get("trace_id") or "") or None,
            thread_id=str(data.get("thread_id") or "") or None,
            parent_message_id=str(data.get("parent_message_id") or "") or None,
            code=str(data.get("code") or "") or None,
            detail=str(data.get("detail") or "") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message_id": self.message_id,
            "status": self.status,
            "event_at": self.event_at,
        }
        if self.from_account_id is not None:
            payload["from_account_id"] = self.from_account_id
        if self.from_session_tag is not None:
            payload["from_session_tag"] = self.from_session_tag
        if self.to_account_id is not None:
            payload["to_account_id"] = self.to_account_id
        if self.trace_id is not None:
            payload["trace_id"] = self.trace_id
        if self.thread_id is not None:
            payload["thread_id"] = self.thread_id
        if self.parent_message_id is not None:
            payload["parent_message_id"] = self.parent_message_id
        if self.code is not None:
            payload["code"] = self.code
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload
