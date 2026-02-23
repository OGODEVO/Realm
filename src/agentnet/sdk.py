"""High-level wrapper SDK for embedding AgentNet in external agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentnet.config import DEFAULT_NATS_URL
from agentnet.node import AgentNode
from agentnet.schema import AgentMessage

ReceiveHandler = Callable[[AgentMessage], Awaitable[None]]


class AgentSDKError(Exception):
    """Base error for SDK operations."""


class AgentRequestError(AgentSDKError):
    """Request failed with a remote error response."""

    def __init__(
        self,
        *,
        code: str,
        detail: str,
        trace_id: str | None = None,
        request_message_id: str | None = None,
    ) -> None:
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.trace_id = trace_id
        self.request_message_id = request_message_id


class AgentBusyError(AgentRequestError):
    pass


class AgentRateLimitedError(AgentRequestError):
    pass


class AgentTimeoutError(AgentRequestError):
    pass


class AgentServiceDegradedError(AgentRequestError):
    pass


class AgentDuplicateError(AgentRequestError):
    pass


class AgentExpiredError(AgentRequestError):
    pass


class AgentHandlerError(AgentRequestError):
    pass


_ERROR_CLASS_BY_CODE: dict[str, type[AgentRequestError]] = {
    "busy": AgentBusyError,
    "shutting_down": AgentBusyError,
    "rate_limited": AgentRateLimitedError,
    "timeout": AgentTimeoutError,
    "service_degraded": AgentServiceDegradedError,
    "duplicate": AgentDuplicateError,
    "expired": AgentExpiredError,
    "missing_ttl": AgentExpiredError,
    "handler_error": AgentHandlerError,
}


class AgentWrapper:
    """Stable adapter API for account-based AgentNet integration."""

    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        username: str | None = None,
        account_id: str | None = None,
        capabilities: list[str] | None = None,
        nats_url: str = DEFAULT_NATS_URL,
        metadata: dict[str, Any] | None = None,
        heartbeat_interval: float = 12.0,
        **node_options: Any,
    ) -> None:
        self._node = AgentNode(
            agent_id=agent_id,
            name=name,
            username=username,
            account_id=account_id,
            capabilities=capabilities,
            nats_url=nats_url,
            metadata=metadata,
            heartbeat_interval=heartbeat_interval,
            **node_options,
        )

    @property
    def node(self) -> AgentNode:
        """Escape hatch for advanced use cases."""
        return self._node

    @property
    def account_id(self) -> str | None:
        return self._node.account_id

    @property
    def username(self) -> str | None:
        return self._node.username

    @property
    def session_tag(self) -> str | None:
        return self._node.session_tag

    def receive(self, handler: ReceiveHandler) -> ReceiveHandler:
        """Register inbound message handler."""
        return self._node.on_message(handler)

    async def connect(self) -> None:
        """Connect and register with the registry."""
        await self._node.start()

    async def close(self) -> None:
        await self._node.close()

    async def resolve_username(self, username: str, timeout: float = 2.0) -> str:
        return await self._node.resolve_account_id_by_username(username, timeout=timeout)

    async def send(
        self,
        *,
        payload: Any,
        to_account_id: str | None = None,
        to_username: str | None = None,
        to_capability: str | None = None,
        kind: str = "direct",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
    ) -> str:
        self._validate_target(to_account_id=to_account_id, to_username=to_username, to_capability=to_capability)
        if to_account_id is not None:
            return await self._node.send_to_account(
                to_account_id=to_account_id,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
            )
        if to_username is not None:
            return await self._node.send_to_username(
                username=to_username,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
            )
        if to_capability is not None:
            return await self._node.send_to_capability(
                capability=to_capability,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
            )
        raise AssertionError("unreachable")

    async def request(
        self,
        *,
        payload: Any,
        to_account_id: str | None = None,
        to_username: str | None = None,
        to_capability: str | None = None,
        timeout: float = 5.0,
        kind: str = "request",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
    ) -> AgentMessage:
        self._validate_target(to_account_id=to_account_id, to_username=to_username, to_capability=to_capability)
        if to_account_id is not None:
            reply = await self._node.request_account(
                to_account_id=to_account_id,
                payload=payload,
                timeout=timeout,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
            )
            self._raise_if_error_reply(reply)
            return reply
        if to_username is not None:
            reply = await self._node.request_username(
                username=to_username,
                payload=payload,
                timeout=timeout,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
            )
            self._raise_if_error_reply(reply)
            return reply
        if to_capability is not None:
            reply = await self._node.request_capability(
                capability=to_capability,
                payload=payload,
                timeout=timeout,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
            )
            self._raise_if_error_reply(reply)
            return reply
        raise AssertionError("unreachable")

    async def reply(
        self,
        *,
        request: AgentMessage,
        payload: Any,
        kind: str = "reply",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
    ) -> str:
        return await self._node.reply(
            request=request,
            payload=payload,
            kind=kind,
            ttl_ms=ttl_ms,
            trace_id=trace_id,
        )

    @staticmethod
    def _validate_target(
        *,
        to_account_id: str | None,
        to_username: str | None,
        to_capability: str | None,
    ) -> None:
        target_count = sum(1 for item in (to_account_id, to_username, to_capability) if item is not None)
        if target_count != 1:
            raise ValueError("Provide exactly one target: to_account_id, to_username, or to_capability.")

    @staticmethod
    def _raise_if_error_reply(reply: AgentMessage) -> None:
        payload = reply.payload
        if not isinstance(payload, dict):
            return
        code = str(payload.get("error") or "").strip()
        if not code:
            return
        detail = str(payload.get("detail") or "").strip()
        request_message_id = str(payload.get("request_message_id") or "") or None
        trace_id = str(payload.get("trace_id") or reply.trace_id or "") or None
        exc_type = _ERROR_CLASS_BY_CODE.get(code, AgentRequestError)
        raise exc_type(
            code=code,
            detail=detail,
            trace_id=trace_id,
            request_message_id=request_message_id,
        )
