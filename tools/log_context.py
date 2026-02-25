"""
Structured logging with contextual metadata (chat_id, request_id).

Usage:
    from tools.log_context import slog, set_context, clear_context

    set_context(chat_id=12345, request_id="abc-123")
    slog.info("something happened", tool="get_live_scores", latency_ms=142)
    clear_context()

All log lines are JSON so they can be parsed by any log aggregator.
Context vars (chat_id, request_id) are automatically injected into every log line
for the current thread/async-task without passing them around manually.
"""

import contextvars
import json
import logging
import time
import uuid
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Context variables — set once per incoming message, readable everywhere
# ---------------------------------------------------------------------------
_chat_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("chat_id", default=None)
_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)


def set_context(chat_id: Optional[int] = None, request_id: Optional[str] = None) -> None:
    """Set per-request context that will be injected into all structured logs."""
    if chat_id is not None:
        _chat_id_var.set(chat_id)
    if request_id is not None:
        _request_id_var.set(request_id)


def clear_context() -> None:
    """Reset context vars after a request is fully handled."""
    _chat_id_var.set(None)
    _request_id_var.set(None)


def new_request_id() -> str:
    """Generate a short, unique request ID (first 8 chars of uuid4)."""
    return uuid.uuid4().hex[:8]


def get_context() -> dict[str, Any]:
    """Return the current context dict (chat_id + request_id)."""
    ctx: dict[str, Any] = {}
    cid = _chat_id_var.get()
    rid = _request_id_var.get()
    if cid is not None:
        ctx["chat_id"] = cid
    if rid is not None:
        ctx["request_id"] = rid
    return ctx


# ---------------------------------------------------------------------------
# Timer helper
# ---------------------------------------------------------------------------
class Timer:
    """Simple context-manager / manual timer that measures elapsed milliseconds."""

    def __init__(self) -> None:
        self._start: float = time.perf_counter()
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: Any) -> None:
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 1)

    def stop(self) -> float:
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 1)
        return self.elapsed_ms


# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------
class _StructuredLogger:
    """
    Thin wrapper that emits JSON log lines through the standard logging module.

    Every call automatically merges in the current context vars so you never
    have to pass chat_id / request_id explicitly.
    """

    def __init__(self, name: str = "brianna1") -> None:
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, event: str, extra: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"event": event}
        payload.update(get_context())
        payload.update(extra)
        # Use standard logger so existing handlers (console, file) still work.
        self._logger.log(level, json.dumps(payload, default=str))

    # Convenience methods matching standard log levels ----------------------

    def debug(self, event: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, event, kw)

    def info(self, event: str, **kw: Any) -> None:
        self._emit(logging.INFO, event, kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._emit(logging.WARNING, event, kw)

    def error(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, kw)


# Module-level singleton — import and use directly
slog = _StructuredLogger()
