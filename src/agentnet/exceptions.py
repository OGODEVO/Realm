"""Typed exception hierarchy for AgentNet.

Leaf module — no internal agentnet imports — safe for node.py and
registry.py to import without creating cycles.

Hierarchy::

    AgentSDKError
    ├── AgentRequestError        (remote agent rejected with an error code)
    │   ├── AgentBusyError
    │   ├── AgentRateLimitedError
    │   ├── AgentTimeoutError
    │   ├── AgentServiceDegradedError
    │   ├── AgentDuplicateError
    │   ├── AgentExpiredError
    │   └── AgentHandlerError
    ├── TransportError           (network / message-delivery layer)
    │   ├── ConnectionError
    │   ├── DeliveryAckTimeout
    │   ├── DeliveryRejected
    │   └── DeliveryAckUnusable
    └── RegistryError            (registry request layer)
        ├── RegistryTimeout
        ├── RegistryProtocolError
        └── RegistryRemoteError
"""

from __future__ import annotations

import uuid
from typing import Any


def _new_error_id() -> str:
    return f"err_{uuid.uuid4().hex[:24]}"


class AgentSDKError(Exception):
    """Root for every error raised by the AgentNet SDK."""


class AgentRequestError(AgentSDKError):
    """A remote agent returned an explicit error reply."""

    def __init__(
        self,
        *,
        code: str,
        detail: str,
        trace_id: str | None = None,
        request_message_id: str | None = None,
        error_instance_id: str | None = None,
    ) -> None:
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.trace_id = trace_id
        self.request_message_id = request_message_id
        self.error_instance_id = error_instance_id or _new_error_id()

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} [{self.error_instance_id}]"


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


ERROR_CLASS_BY_CODE: dict[str, type[AgentRequestError]] = {
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


class TransportError(AgentSDKError):
    """Network or message-delivery failure (no remote error reply)."""

    def __init__(
        self,
        message: str,
        *,
        message_id: str | None = None,
        error_instance_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message_id = message_id
        self.error_instance_id = error_instance_id or _new_error_id()

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} [{self.error_instance_id}]"


class ConnectionError(TransportError):
    """Cannot reach the NATS server."""


class DeliveryAckTimeout(TransportError):
    """Recipient did not acknowledge the message within the retry window."""


class DeliveryRejected(TransportError):
    """Recipient explicitly rejected the message."""


class DeliveryAckUnusable(TransportError):
    """Delivery receipt returned an unusable status."""


class RegistryError(AgentSDKError):
    """Base for registry request failures."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        error_instance_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.error_instance_id = error_instance_id or _new_error_id()

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} [{self.error_instance_id}]"


class RegistryTimeout(RegistryError):
    """Registry did not respond within the timeout."""


class RegistryProtocolError(RegistryError):
    """Registry response was malformed or missing required fields."""


class RegistryRemoteError(RegistryError):
    """Registry returned an application-level error."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        code: str | None = None,
        error_instance_id: str | None = None,
    ) -> None:
        super().__init__(message, operation=operation, error_instance_id=error_instance_id)
        self.code = code


def registry_timeout(operation: str) -> RegistryTimeout:
    return RegistryTimeout(
        f"Registry did not respond to {operation}",
        operation=operation,
    )


def registry_protocol_error(operation: str, detail: str) -> RegistryProtocolError:
    return RegistryProtocolError(
        f"{operation}: {detail}",
        operation=operation,
    )


def registry_remote_error(operation: str, raw_error: Any) -> RegistryRemoteError:
    code = str(raw_error or f"{operation}_failed")
    return RegistryRemoteError(
        code,
        operation=operation,
        code=code,
    )


def connection_error(nats_url: str) -> ConnectionError:
    return ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?")
