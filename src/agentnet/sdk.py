"""High-level wrapper SDK for embedding AgentNet in external agents."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

from agentnet.config import DEFAULT_NATS_URL
from agentnet.node import AgentNode
from agentnet.registry import get_thread_status
from agentnet.schema import AgentInfo, AgentMessage

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


@dataclass(slots=True)
class SDKResult:
    """Unified high-level SDK response envelope."""

    ok: bool
    thread_id: str | None
    message_id: str | None
    parent_message_id: str | None
    text: str | None = None
    data: Any = None
    error: str | None = None
    trace_id: str | None = None


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("text")
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        err = payload.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
    if isinstance(payload, str):
        normalized = payload.strip()
        return normalized or None
    return None


def _parse_target_value(target: str) -> tuple[str, str]:
    normalized = str(target or "").strip()
    if not normalized:
        raise ValueError("target is required")
    lowered = normalized.lower()
    if lowered.startswith("account:"):
        account_id = normalized.split(":", 1)[1].strip()
        if not account_id:
            raise ValueError("account target cannot be empty")
        return "account", account_id
    if lowered.startswith("capability:"):
        capability = normalized.split(":", 1)[1].strip()
        if not capability:
            raise ValueError("capability target cannot be empty")
        return "capability", capability
    if normalized.startswith("@"):
        username = normalized[1:].strip()
        if not username:
            raise ValueError("username target cannot be empty")
        return "username", username
    if normalized.startswith("acct_"):
        return "account", normalized
    return "username", normalized


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
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
        require_delivery_ack: bool = True,
        retry_attempts: int | None = None,
        receipt_timeout: float | None = None,
    ) -> str:
        self._validate_target(to_account_id=to_account_id, to_username=to_username, to_capability=to_capability)
        if to_account_id is not None:
            return await self._node.send_to_account(
                to_account_id=to_account_id,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
        if to_username is not None:
            return await self._node.send_to_username(
                username=to_username,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
        if to_capability is not None:
            return await self._node.send_to_capability(
                capability=to_capability,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
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
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
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
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
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
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
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
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
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
        thread_id: str | None = None,
        parent_message_id: str | None = None,
    ) -> str:
        return await self._node.reply(
            request=request,
            payload=payload,
            kind=kind,
            ttl_ms=ttl_ms,
            trace_id=trace_id,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
        )

    async def search_profiles(
        self,
        *,
        query: str = "",
        capability: str | None = None,
        limit: int = 20,
        online_only: bool = False,
        timeout: float = 2.0,
    ) -> list[dict[str, Any]]:
        return await self._node.search_profiles(
            query=query,
            capability=capability,
            limit=limit,
            online_only=online_only,
            timeout=timeout,
        )

    async def get_profile(
        self,
        *,
        account_id: str | None = None,
        username: str | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._node.get_profile(account_id=account_id, username=username, timeout=timeout)

    async def list_threads(
        self,
        *,
        participant_account_id: str | None = None,
        participant_username: str | None = None,
        query: str = "",
        limit: int = 20,
        soft_limit_tokens: int | None = None,
        hard_limit_tokens: int | None = None,
        timeout: float = 2.0,
    ) -> list[dict[str, Any]]:
        return await self._node.list_threads(
            participant_account_id=participant_account_id,
            participant_username=participant_username,
            query=query,
            limit=limit,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            timeout=timeout,
        )

    async def get_thread_messages(
        self,
        *,
        thread_id: str,
        limit: int = 50,
        cursor: str | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._node.get_thread_messages(
            thread_id=thread_id,
            limit=limit,
            cursor=cursor,
            timeout=timeout,
        )

    async def search_messages(
        self,
        *,
        thread_id: str | None = None,
        from_account_id: str | None = None,
        to_account_id: str | None = None,
        kind: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._node.search_messages(
            thread_id=thread_id,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            kind=kind,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            cursor=cursor,
            timeout=timeout,
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
        _raise_if_error_reply(reply)


class ThreadSession:
    """Thread-scoped helper that auto-carries parent_message_id."""

    def __init__(self, sdk: AgentSDK, thread_id: str, parent_message_id: str | None = None) -> None:
        self._sdk = sdk
        self.thread_id = str(thread_id or "").strip()
        if not self.thread_id:
            raise ValueError("thread_id is required")
        self.parent_message_id = parent_message_id

    def set_parent(self, parent_message_id: str | None) -> None:
        self.parent_message_id = parent_message_id

    async def send_text(
        self,
        to: str,
        text: str,
        *,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        parent = parent_message_id if parent_message_id is not None else self.parent_message_id
        result = await self._sdk.send_text(
            to,
            text,
            thread_id=self.thread_id,
            parent_message_id=parent,
            idempotency_key=idempotency_key,
        )
        self.parent_message_id = result.message_id or self.parent_message_id
        return result

    async def ask_text(
        self,
        to: str,
        text: str,
        *,
        timeout: float | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        parent = parent_message_id if parent_message_id is not None else self.parent_message_id
        result = await self._sdk.ask_text(
            to,
            text,
            thread_id=self.thread_id,
            timeout=timeout,
            parent_message_id=parent,
            idempotency_key=idempotency_key,
        )
        self.parent_message_id = result.message_id or self.parent_message_id
        return result

    async def send_json(
        self,
        to: str,
        data: Any,
        *,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        parent = parent_message_id if parent_message_id is not None else self.parent_message_id
        result = await self._sdk.send_json(
            to,
            data,
            thread_id=self.thread_id,
            parent_message_id=parent,
            idempotency_key=idempotency_key,
        )
        self.parent_message_id = result.message_id or self.parent_message_id
        return result

    async def ask_json(
        self,
        to: str,
        data: Any,
        *,
        timeout: float | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        parent = parent_message_id if parent_message_id is not None else self.parent_message_id
        result = await self._sdk.ask_json(
            to,
            data,
            thread_id=self.thread_id,
            timeout=timeout,
            parent_message_id=parent,
            idempotency_key=idempotency_key,
        )
        self.parent_message_id = result.message_id or self.parent_message_id
        return result


class AgentSDK:
    """Ergonomic high-level SDK over AgentNet AgentNode."""

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
        default_request_timeout: float = 60.0,
        default_thread_prefix: str = "thread_sdk",
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
        self.default_request_timeout = max(0.1, float(default_request_timeout))
        prefix = str(default_thread_prefix or "thread_sdk").strip()
        self.default_thread_prefix = prefix or "thread_sdk"

    @property
    def node(self) -> AgentNode:
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
        return self._node.on_message(handler)

    async def start(self) -> None:
        await self._node.start()

    async def stop(self) -> None:
        await self._node.close()

    async def __aenter__(self) -> AgentSDK:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    def new_thread_id(self) -> str:
        from agentnet.utils import new_ulid

        return f"{self.default_thread_prefix}_{new_ulid().lower()}"

    def thread(self, thread_id: str, parent_message_id: str | None = None) -> ThreadSession:
        return ThreadSession(self, thread_id=thread_id, parent_message_id=parent_message_id)

    async def list_online(self, timeout: float = 2.0) -> list[AgentInfo]:
        return await self._node.list_online_agents(timeout=timeout)

    async def get_profile(
        self,
        target: str | None = None,
        *,
        account_id: str | None = None,
        username: str | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        if target:
            kind, value = _parse_target_value(target)
            if kind == "account":
                account_id = value
            elif kind == "username":
                username = value
            else:
                raise ValueError("capability target is not valid for get_profile")
        return await self._node.get_profile(account_id=account_id, username=username, timeout=timeout)

    async def thread_status(
        self,
        thread_id: str,
        *,
        soft_limit_tokens: int | None = None,
        hard_limit_tokens: int | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await get_thread_status(
            self._node.nats_url,
            thread_id=thread_id,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            timeout=timeout,
        )

    async def list_threads(
        self,
        *,
        participant_account_id: str | None = None,
        participant_username: str | None = None,
        query: str = "",
        limit: int = 20,
        soft_limit_tokens: int | None = None,
        hard_limit_tokens: int | None = None,
        timeout: float = 2.0,
    ) -> list[dict[str, Any]]:
        return await self._node.list_threads(
            participant_account_id=participant_account_id,
            participant_username=participant_username,
            query=query,
            limit=limit,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            timeout=timeout,
        )

    async def get_thread_messages(
        self,
        *,
        thread_id: str,
        limit: int = 50,
        cursor: str | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._node.get_thread_messages(
            thread_id=thread_id,
            limit=limit,
            cursor=cursor,
            timeout=timeout,
        )

    async def search_messages(
        self,
        *,
        thread_id: str | None = None,
        from_account_id: str | None = None,
        to_account_id: str | None = None,
        kind: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._node.search_messages(
            thread_id=thread_id,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            kind=kind,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            cursor=cursor,
            timeout=timeout,
        )

    async def send_text(
        self,
        to: str,
        text: str,
        *,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        return await self.send_json(
            to,
            {"text": str(text)},
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
        )

    async def ask_text(
        self,
        to: str,
        text: str,
        *,
        thread_id: str | None = None,
        timeout: float | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        return await self.ask_json(
            to,
            {"text": str(text)},
            thread_id=thread_id,
            timeout=timeout,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
        )

    async def send_json(
        self,
        to: str,
        data: Any,
        *,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
        require_delivery_ack: bool = True,
        retry_attempts: int | None = None,
        receipt_timeout: float | None = None,
    ) -> SDKResult:
        kind, value = _parse_target_value(to)
        effective_thread_id = self._normalize_thread_id(thread_id)
        message_id = await self._send_by_target(
            target_kind=kind,
            target_value=value,
            payload=data,
            thread_id=effective_thread_id,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
            require_delivery_ack=require_delivery_ack,
            retry_attempts=retry_attempts,
            receipt_timeout=receipt_timeout,
        )
        return SDKResult(
            ok=True,
            thread_id=effective_thread_id,
            message_id=message_id,
            parent_message_id=parent_message_id,
            data=None,
            text=None,
        )

    async def ask_json(
        self,
        to: str,
        data: Any,
        *,
        thread_id: str | None = None,
        timeout: float | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SDKResult:
        kind, value = _parse_target_value(to)
        effective_thread_id = self._normalize_thread_id(thread_id)
        reply = await self._request_by_target(
            target_kind=kind,
            target_value=value,
            payload=data,
            timeout=timeout if timeout is not None else self.default_request_timeout,
            thread_id=effective_thread_id,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
        )
        _raise_if_error_reply(reply)
        return SDKResult(
            ok=True,
            thread_id=reply.thread_id or effective_thread_id,
            message_id=reply.message_id or None,
            parent_message_id=reply.parent_message_id or parent_message_id,
            text=_extract_text(reply.payload),
            data=reply.payload,
            trace_id=reply.trace_id,
        )

    def _normalize_thread_id(self, thread_id: str | None) -> str:
        value = str(thread_id or "").strip()
        if value:
            return value
        return self.new_thread_id()

    async def _send_by_target(
        self,
        *,
        target_kind: str,
        target_value: str,
        payload: Any,
        thread_id: str,
        parent_message_id: str | None,
        idempotency_key: str | None,
        require_delivery_ack: bool,
        retry_attempts: int | None,
        receipt_timeout: float | None,
    ) -> str:
        if target_kind == "username":
            return await self._node.send_to_username(
                username=target_value,
                payload=payload,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
        if target_kind == "account":
            return await self._node.send_to_account(
                to_account_id=target_value,
                payload=payload,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
        if target_kind == "capability":
            return await self._node.send_to_capability(
                capability=target_value,
                payload=payload,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
        raise ValueError(f"unsupported target kind: {target_kind}")

    async def _request_by_target(
        self,
        *,
        target_kind: str,
        target_value: str,
        payload: Any,
        timeout: float,
        thread_id: str,
        parent_message_id: str | None,
        idempotency_key: str | None,
    ) -> AgentMessage:
        safe_timeout = max(0.1, float(timeout))
        if target_kind == "username":
            return await self._node.request_username(
                username=target_value,
                payload=payload,
                timeout=safe_timeout,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
        if target_kind == "account":
            return await self._node.request_account(
                to_account_id=target_value,
                payload=payload,
                timeout=safe_timeout,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
        if target_kind == "capability":
            return await self._node.request_capability(
                capability=target_value,
                payload=payload,
                timeout=safe_timeout,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
        raise ValueError(f"unsupported target kind: {target_kind}")
