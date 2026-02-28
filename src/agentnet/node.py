"""High-level AgentNet node API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from agentnet.config import DEFAULT_NATS_URL
from agentnet.dev_auth import (
    DEV_AUTH_SCHEME,
    build_message_claims,
    build_register_claims,
    load_or_create_dev_keypair,
    parse_bool,
    sign_claims,
    verify_claims,
)
from agentnet.registry import (
    get_profile,
    get_profile_with_client,
    get_thread_messages,
    get_thread_messages_with_client,
    list_threads,
    list_threads_with_client,
    list_online_agents,
    list_online_agents_with_client,
    resolve_dev_public_key_by_account,
    resolve_dev_public_key_by_account_with_client,
    resolve_account_by_username,
    resolve_account_by_username_with_client,
    search_messages,
    search_messages_with_client,
    search_profiles,
    search_profiles_with_client,
)
from agentnet.schema import AgentInfo, AgentMessage, DeliveryReceipt
from agentnet.subjects import (
    REGISTRY_GOODBYE_SUBJECT,
    REGISTRY_HELLO_SUBJECT,
    REGISTRY_REGISTER_SUBJECT,
    account_receipts_subject,
    account_inbox_subject,
    agent_capability_subject,
)
from agentnet.utils import decode_json, encode_json, new_id, new_ulid, utc_now_iso

MessageHandler = Callable[[AgentMessage], Awaitable[None]]


class AgentNode:
    def __init__(
        self,
        agent_id: str,
        name: str,
        account_id: str | None = None,
        username: str | None = None,
        capabilities: list[str] | None = None,
        nats_url: str = DEFAULT_NATS_URL,
        metadata: dict[str, Any] | None = None,
        heartbeat_interval: float = 12.0,
        logger: logging.Logger | None = None,
        max_concurrency: int = 4,
        max_pending: int = 100,
        max_payload_bytes: int = 256_000,
        work_timeout_seconds: float = 30.0,
        drain_timeout_seconds: float = 20.0,
        default_ttl_ms: int = 30_000,
        dedupe_ttl_seconds: float = 600.0,
        rate_limit_per_sender_per_sec: float = 5.0,
        rate_limit_burst: int = 10,
        circuit_breaker_failures: int = 5,
        circuit_breaker_reset_seconds: float = 15.0,
        default_send_retry_attempts: int = 2,
        default_receipt_timeout_seconds: float = 1.5,
        default_schema_version: str = "1.1",
        supported_schema_major: int = 1,
        dev_auth_enabled: bool | None = None,
        dev_auth_key_dir: str | None = None,
        dev_auth_key_cache_seconds: float = 300.0,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.account_id = account_id
        self.username = username
        self.capabilities = capabilities or []
        self.metadata = metadata or {}
        self.nats_url = nats_url
        self.heartbeat_interval = max(1.0, heartbeat_interval)
        self.logger = logger or logging.getLogger(f"agentnet.{agent_id}")

        self.max_concurrency = max(1, max_concurrency)
        self.max_pending = max(1, max_pending)
        self.max_payload_bytes = max(1, max_payload_bytes)
        self.work_timeout_seconds = max(0.1, work_timeout_seconds)
        self.drain_timeout_seconds = max(0.1, drain_timeout_seconds)
        self.default_ttl_ms = max(1, default_ttl_ms)
        self.dedupe_ttl_seconds = max(1.0, dedupe_ttl_seconds)
        self.rate_limit_per_sender_per_sec = max(0.1, rate_limit_per_sender_per_sec)
        self.rate_limit_burst = max(1, rate_limit_burst)
        self.circuit_breaker_failures = max(1, circuit_breaker_failures)
        self.circuit_breaker_reset_seconds = max(1.0, circuit_breaker_reset_seconds)
        self.default_send_retry_attempts = max(0, default_send_retry_attempts)
        self.default_receipt_timeout_seconds = max(0.2, default_receipt_timeout_seconds)
        self.default_schema_version = str(default_schema_version or "1.1")
        self.supported_schema_major = max(1, int(supported_schema_major))
        self.dev_auth_enabled = (
            parse_bool(os.getenv("DEV_AUTH"), default=False) if dev_auth_enabled is None else bool(dev_auth_enabled)
        )
        self.dev_auth_key_dir = dev_auth_key_dir or os.getenv("DEV_AUTH_KEY_DIR") or ".keys"
        self.dev_auth_key_cache_seconds = max(5.0, dev_auth_key_cache_seconds)
        self._dev_private_key: str | None = None
        self._dev_public_key: str | None = None
        self._dev_public_key_cache: dict[str, tuple[str, float]] = {}

        self._nc: NATS | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._incoming_queue: asyncio.Queue[AgentMessage] | None = None
        self._pending_receipts: dict[str, asyncio.Future[DeliveryReceipt]] = {}
        self._message_handler: MessageHandler | None = None
        self._accepting_messages = False
        self.session_tag: str | None = None

        self._sender_buckets: dict[str, tuple[float, float]] = {}
        self._seen_message_ids: dict[str, float] = {}
        self._seen_idempotency_keys: dict[str, float] = {}

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        self._inflight_count = 0
        self._processed_count = 0
        self._dropped_count = 0
        self._timeout_count = 0
        self._error_count = 0
        self._rate_limited_count = 0
        self._expired_count = 0
        self._payload_rejected_count = 0
        self._duplicate_count = 0
        self._busy_count = 0
        self._retry_count = 0
        self._receipt_timeout_count = 0
        self._handle_time_total_ms = 0.0
        self._metrics_window_size = max(32, int(os.getenv("AGENT_METRICS_WINDOW_SIZE", "1024")))
        self._latency_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._metrics_window_size)
        )
        self._username_resolve_cache_ttl_seconds = max(
            0.0, float(os.getenv("ACCOUNT_RESOLVE_CACHE_TTL_SECONDS", "60"))
        )
        self._username_account_cache: dict[str, tuple[str, float]] = {}
        self._username_cache_hits = 0
        self._username_cache_misses = 0

    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            agent_id=self.agent_id,
            name=self.name,
            account_id=self.account_id,
            username=self.username,
            session_tag=self.session_tag,
            capabilities=self.capabilities,
            metadata=self.metadata,
            last_seen=utc_now_iso(),
        )

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        self._message_handler = handler
        return handler

    def metrics_snapshot(self) -> dict[str, Any]:
        pending_count = self._incoming_queue.qsize() if self._incoming_queue is not None else 0
        avg_ms = (self._handle_time_total_ms / self._processed_count) if self._processed_count else 0.0
        self._prune_username_cache(time.monotonic())
        return {
            "account_id": self.account_id,
            "username": self.username,
            "agent_id": self.agent_id,
            "session_tag": self.session_tag,
            "inflight_count": self._inflight_count,
            "pending_count": pending_count,
            "processed_count": self._processed_count,
            "dropped_count": self._dropped_count,
            "timeout_count": self._timeout_count,
            "error_count": self._error_count,
            "rate_limited_count": self._rate_limited_count,
            "expired_count": self._expired_count,
            "payload_rejected_count": self._payload_rejected_count,
            "duplicate_count": self._duplicate_count,
            "busy_count": self._busy_count,
            "retry_count": self._retry_count,
            "receipt_timeout_count": self._receipt_timeout_count,
            "avg_handle_ms": round(avg_ms, 2),
            "circuit_open": self._is_circuit_open(),
            "latency_ms": {
                "send_account": self._latency_stats("send_account"),
                "request_account": self._latency_stats("request_account"),
                "resolve_username": self._latency_stats("resolve_username"),
            },
            "username_cache": {
                "ttl_seconds": self._username_resolve_cache_ttl_seconds,
                "size": len(self._username_account_cache),
                "hits": self._username_cache_hits,
                "misses": self._username_cache_misses,
            },
        }

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float:
        if not samples:
            return 0.0
        if percentile <= 0:
            return samples[0]
        if percentile >= 100:
            return samples[-1]
        idx = int(round((percentile / 100.0) * (len(samples) - 1)))
        idx = max(0, min(idx, len(samples) - 1))
        return samples[idx]

    def _record_latency(self, metric_name: str, elapsed_ms: float) -> None:
        if elapsed_ms < 0:
            elapsed_ms = 0.0
        self._latency_samples[metric_name].append(float(elapsed_ms))

    def _latency_stats(self, metric_name: str) -> dict[str, float | int]:
        samples = list(self._latency_samples.get(metric_name, ()))
        if not samples:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
        samples.sort()
        return {
            "count": len(samples),
            "p50": round(self._percentile(samples, 50), 2),
            "p95": round(self._percentile(samples, 95), 2),
            "p99": round(self._percentile(samples, 99), 2),
        }

    def _prune_username_cache(self, now: float | None = None) -> None:
        if self._username_resolve_cache_ttl_seconds <= 0 or not self._username_account_cache:
            return
        check_at = now if now is not None else time.monotonic()
        expired = [k for k, (_, expires_at) in self._username_account_cache.items() if expires_at <= check_at]
        for key in expired:
            self._username_account_cache.pop(key, None)

    async def start(self) -> None:
        if self._nc and self._nc.is_connected:
            return

        self._nc = NATS()
        await self._nc.connect(servers=[self.nats_url], name=f"agentnet-{self.agent_id}")
        try:
            self._ensure_dev_auth_identity()
            await self._register()
            if not self.account_id:
                raise RuntimeError("Account identity missing after register.")
            await self._nc.subscribe(
                account_receipts_subject(self.account_id),
                queue=f"agentnet-receipts-{self.account_id}",
                cb=self._handle_receipt,
            )
            if self._message_handler is not None:
                self._start_workers()
                self._accepting_messages = True
                await self._nc.subscribe(
                    account_inbox_subject(self.account_id),
                    queue=f"agentnet-account-{self.account_id}",
                    cb=self._handle_inbox,
                )
                for capability in self.capabilities:
                    await self._nc.subscribe(
                        agent_capability_subject(capability),
                        queue=f"agentnet-capability-{capability}",
                        cb=self._handle_inbox,
                    )
            elif self.capabilities:
                self.logger.warning("Capabilities were provided but no message handler is set; subscriptions disabled.")
            await self._publish_hello()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"agentnet-heartbeat-{self.agent_id}",
            )
        except Exception:
            self._accepting_messages = False
            await self._stop_workers()
            if self._nc and self._nc.is_connected:
                await self._nc.drain()
            self._nc = None
            self.session_tag = None
            raise

    async def start_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.close()

    async def close(self) -> None:
        self._accepting_messages = False
        await self._stop_workers()

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if not self._nc:
            self.session_tag = None
            return

        nc = self._nc
        self._nc = None
        if nc.is_connected:
            try:
                await self._publish_goodbye(client=nc)
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed publishing registry.goodbye")
            await nc.drain()

        self.logger.info("AgentNode shutdown metrics=%s", self.metrics_snapshot())
        for pending in self._pending_receipts.values():
            if not pending.done():
                pending.cancel()
        self._pending_receipts.clear()
        self.session_tag = None

    async def send(
        self,
        to: str,
        payload: Any,
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
        explicit_account_id = self._parse_account_target(to)
        if explicit_account_id:
            return await self.send_to_account(
                explicit_account_id,
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
        explicit_username = self._parse_username_target(to)
        if explicit_username:
            return await self.send_to_username(
                explicit_username,
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
        raise ValueError(
            "Routing target must be account:<account_id>, acct_..., username:<name>, or @name. "
            "Use send_to_capability() for capability routing."
        )

    async def send_to_account(
        self,
        to_account_id: str,
        payload: Any,
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
        started = time.perf_counter()
        account_id = to_account_id.strip()
        if not account_id:
            raise ValueError("to_account_id is required")
        try:
            nc = self._require_connected_client()
            envelope = self._build_outbound_message(
                to=account_id,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                to_account_id=account_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
            await self._publish_with_retry(
                nc=nc,
                subject=account_inbox_subject(account_id),
                envelope=envelope,
                require_delivery_ack=require_delivery_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
            return envelope.message_id
        finally:
            self._record_latency("send_account", (time.perf_counter() - started) * 1000.0)

    async def send_to_username(
        self,
        username: str,
        payload: Any,
        kind: str = "direct",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
        require_delivery_ack: bool = True,
        retry_attempts: int | None = None,
        receipt_timeout: float | None = None,
        timeout: float = 2.0,
    ) -> str:
        account_id = await self.resolve_account_id_by_username(username, timeout=timeout)
        return await self.send_to_account(
            to_account_id=account_id,
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

    async def send_to_capability(
        self,
        capability: str,
        payload: Any,
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
        nc = self._require_connected_client()
        envelope = self._build_outbound_message(
            to=f"capability:{capability}",
            payload=payload,
            kind=kind,
            ttl_ms=ttl_ms,
            trace_id=trace_id,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
        )
        await self._publish_with_retry(
            nc=nc,
            subject=agent_capability_subject(capability),
            envelope=envelope,
            require_delivery_ack=require_delivery_ack,
            retry_attempts=retry_attempts,
            receipt_timeout=receipt_timeout,
        )
        return envelope.message_id

    async def request(
        self,
        to: str,
        payload: Any,
        timeout: float = 5.0,
        kind: str = "request",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentMessage:
        explicit_account_id = self._parse_account_target(to)
        if explicit_account_id:
            return await self.request_account(
                explicit_account_id,
                payload=payload,
                timeout=timeout,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
        explicit_username = self._parse_username_target(to)
        if explicit_username:
            return await self.request_username(
                explicit_username,
                payload=payload,
                timeout=timeout,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
        raise ValueError(
            "Routing target must be account:<account_id>, acct_..., username:<name>, or @name. "
            "Use request_capability() for capability routing."
        )

    async def request_account(
        self,
        to_account_id: str,
        payload: Any,
        timeout: float = 5.0,
        kind: str = "request",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentMessage:
        started = time.perf_counter()
        account_id = to_account_id.strip()
        if not account_id:
            raise ValueError("to_account_id is required")
        try:
            nc = self._require_connected_client()
            envelope = self._build_outbound_message(
                to=account_id,
                payload=payload,
                kind=kind,
                ttl_ms=ttl_ms,
                trace_id=trace_id,
                to_account_id=account_id,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                idempotency_key=idempotency_key,
            )
            reply_msg = await nc.request(account_inbox_subject(account_id), self._encode_message(envelope), timeout=timeout)
            data = decode_json(reply_msg.data)
            if not isinstance(data, dict):
                raise ValueError("reply message payload must be a JSON object")
            return AgentMessage.from_dict(data)
        finally:
            self._record_latency("request_account", (time.perf_counter() - started) * 1000.0)

    async def request_username(
        self,
        username: str,
        payload: Any,
        timeout: float = 5.0,
        kind: str = "request",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
        lookup_timeout: float = 2.0,
    ) -> AgentMessage:
        account_id = await self.resolve_account_id_by_username(username, timeout=lookup_timeout)
        return await self.request_account(
            to_account_id=account_id,
            payload=payload,
            timeout=timeout,
            kind=kind,
            ttl_ms=ttl_ms,
            trace_id=trace_id,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
        )

    async def reply(
        self,
        request: AgentMessage,
        payload: Any,
        kind: str = "reply",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
    ) -> str:
        if not request.reply_to:
            raise ValueError("Cannot reply: incoming message has no reply_to subject.")
        nc = self._require_connected_client()
        to_account_id = request.from_account_id
        to_target = to_account_id or request.from_agent or "unknown"
        envelope = self._build_outbound_message(
            to=to_target,
            payload=payload,
            kind=kind,
            ttl_ms=ttl_ms,
            trace_id=trace_id or request.trace_id or request.message_id,
            to_account_id=to_account_id,
            thread_id=thread_id or request.thread_id,
            parent_message_id=parent_message_id or request.message_id,
        )
        await nc.publish(request.reply_to, self._encode_message(envelope))
        return envelope.message_id

    async def resolve_account_id_by_username(self, username: str, timeout: float = 2.0) -> str:
        started = time.perf_counter()
        target = username.strip().lower().lstrip("@")
        if not target:
            raise ValueError("username is required")
        now_mono = time.monotonic()
        self._prune_username_cache(now_mono)
        cached = self._username_account_cache.get(target)
        if cached is not None:
            account_id, expires_at = cached
            if self._username_resolve_cache_ttl_seconds <= 0 or expires_at > now_mono:
                self._username_cache_hits += 1
                self._record_latency("resolve_username", (time.perf_counter() - started) * 1000.0)
                return account_id
            self._username_account_cache.pop(target, None)
        self._username_cache_misses += 1
        # Prefer dedicated resolve RPC; fall back to list scan for compatibility with older registries.
        try:
            if self._nc and self._nc.is_connected:
                account_id, _ = await resolve_account_by_username_with_client(self._nc, target, timeout=timeout)
            else:
                account_id, _ = await resolve_account_by_username(self.nats_url, target, timeout=timeout)
            if self._username_resolve_cache_ttl_seconds > 0:
                self._username_account_cache[target] = (
                    account_id,
                    time.monotonic() + self._username_resolve_cache_ttl_seconds,
                )
            self._record_latency("resolve_username", (time.perf_counter() - started) * 1000.0)
            return account_id
        except Exception:  # noqa: BLE001
            agents = await self.list_online_agents(timeout=timeout)
            for agent in agents:
                if agent.account_id and agent.username and agent.username.lower() == target:
                    if self._username_resolve_cache_ttl_seconds > 0:
                        self._username_account_cache[target] = (
                            agent.account_id,
                            time.monotonic() + self._username_resolve_cache_ttl_seconds,
                        )
                    self._record_latency("resolve_username", (time.perf_counter() - started) * 1000.0)
                    return agent.account_id
        self._record_latency("resolve_username", (time.perf_counter() - started) * 1000.0)
        raise RuntimeError(f"No account found for username '{target}'")

    async def request_capability(
        self,
        capability: str,
        payload: Any,
        timeout: float = 5.0,
        kind: str = "request",
        ttl_ms: int | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentMessage:
        nc = self._require_connected_client()
        envelope = self._build_outbound_message(
            to=f"capability:{capability}",
            payload=payload,
            kind=kind,
            ttl_ms=ttl_ms,
            trace_id=trace_id,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            idempotency_key=idempotency_key,
        )
        reply_msg = await nc.request(agent_capability_subject(capability), self._encode_message(envelope), timeout=timeout)
        data = decode_json(reply_msg.data)
        if not isinstance(data, dict):
            raise ValueError("reply message payload must be a JSON object")
        return AgentMessage.from_dict(data)

    async def list_online_agents(self, timeout: float = 2.0) -> list[AgentInfo]:
        if self._nc and self._nc.is_connected:
            return await list_online_agents_with_client(self._nc, timeout=timeout)
        return await list_online_agents(self.nats_url, timeout=timeout)

    async def search_profiles(
        self,
        *,
        query: str = "",
        capability: str | None = None,
        limit: int = 20,
        online_only: bool = False,
        timeout: float = 2.0,
    ) -> list[dict[str, Any]]:
        if self._nc and self._nc.is_connected:
            return await search_profiles_with_client(
                self._nc,
                query=query,
                capability=capability,
                limit=limit,
                online_only=online_only,
                timeout=timeout,
            )
        return await search_profiles(
            self.nats_url,
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
        if self._nc and self._nc.is_connected:
            return await get_profile_with_client(
                self._nc,
                account_id=account_id,
                username=username,
                timeout=timeout,
            )
        return await get_profile(
            self.nats_url,
            account_id=account_id,
            username=username,
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
        if self._nc and self._nc.is_connected:
            return await list_threads_with_client(
                self._nc,
                participant_account_id=participant_account_id,
                participant_username=participant_username,
                query=query,
                limit=limit,
                soft_limit_tokens=soft_limit_tokens,
                hard_limit_tokens=hard_limit_tokens,
                timeout=timeout,
            )
        return await list_threads(
            self.nats_url,
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
        if self._nc and self._nc.is_connected:
            return await get_thread_messages_with_client(
                self._nc,
                thread_id=thread_id,
                limit=limit,
                cursor=cursor,
                timeout=timeout,
            )
        return await get_thread_messages(
            self.nats_url,
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
        if self._nc and self._nc.is_connected:
            return await search_messages_with_client(
                self._nc,
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
        return await search_messages(
            self.nats_url,
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

    async def _handle_receipt(self, msg: Msg) -> None:
        try:
            data = decode_json(msg.data)
            if not isinstance(data, dict):
                return
            receipt = DeliveryReceipt.from_dict(data)
        except Exception:  # noqa: BLE001
            return
        if not receipt.message_id:
            return
        pending = self._pending_receipts.get(receipt.message_id)
        if pending is None or pending.done():
            return
        pending.set_result(receipt)

    async def _publish_with_retry(
        self,
        *,
        nc: NATS,
        subject: str,
        envelope: AgentMessage,
        require_delivery_ack: bool,
        retry_attempts: int | None,
        receipt_timeout: float | None,
    ) -> None:
        raw = self._encode_message(envelope)
        if not require_delivery_ack:
            await nc.publish(subject, raw)
            return

        max_attempts = 1 + (self.default_send_retry_attempts if retry_attempts is None else max(0, int(retry_attempts)))
        wait_timeout = self.default_receipt_timeout_seconds if receipt_timeout is None else max(0.2, float(receipt_timeout))
        receipt_future = self._pending_receipts.get(envelope.message_id)
        if receipt_future is None or receipt_future.done():
            receipt_future = asyncio.get_running_loop().create_future()
            self._pending_receipts[envelope.message_id] = receipt_future

        try:
            for attempt in range(1, max_attempts + 1):
                if attempt > 1:
                    self._retry_count += 1
                await nc.publish(subject, raw)
                try:
                    receipt = await asyncio.wait_for(asyncio.shield(receipt_future), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    self._receipt_timeout_count += 1
                    if attempt >= max_attempts:
                        raise RuntimeError(
                            f"delivery_ack_timeout message_id={envelope.message_id} attempts={max_attempts}"
                        ) from None
                    await asyncio.sleep(min(0.1 * attempt, 0.5))
                    continue

                status = receipt.status.strip().lower()
                if status in {"accepted", "processed"}:
                    return
                if status == "rejected":
                    if (receipt.code or "").strip().lower() == "duplicate":
                        return
                    code = receipt.code or "rejected"
                    detail = receipt.detail or "delivery rejected by recipient"
                    raise RuntimeError(f"delivery_rejected {code}: {detail}")
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"delivery_ack_unusable status={status or 'unknown'} message_id={envelope.message_id}"
                    )
        finally:
            self._pending_receipts.pop(envelope.message_id, None)

    async def _emit_delivery_receipt(
        self,
        *,
        message: AgentMessage,
        status: str,
        code: str | None = None,
        detail: str | None = None,
    ) -> None:
        sender_account = str(message.from_account_id or "").strip()
        if not sender_account:
            return
        nc = self._nc
        if nc is None or not nc.is_connected:
            return
        receipt = DeliveryReceipt(
            message_id=message.message_id,
            status=status,
            event_at=utc_now_iso(),
            from_account_id=self.account_id,
            from_session_tag=self.session_tag,
            to_account_id=sender_account,
            trace_id=message.trace_id or message.message_id,
            thread_id=message.thread_id,
            parent_message_id=message.parent_message_id,
            code=code,
            detail=detail,
        )
        try:
            await nc.publish(account_receipts_subject(sender_account), encode_json(receipt.to_dict()))
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed emitting delivery receipt status=%s message_id=%s", status, message.message_id)

    async def _handle_inbox(self, msg: Msg) -> None:
        if self._message_handler is None:
            return

        if len(msg.data) > self.max_payload_bytes:
            self._payload_rejected_count += 1
            self._dropped_count += 1
            self.logger.warning("Dropped oversized payload bytes=%s max=%s", len(msg.data), self.max_payload_bytes)
            return

        try:
            data = decode_json(msg.data)
            if not isinstance(data, dict):
                raise ValueError("message payload must be a JSON object")
            message = AgentMessage.from_dict(data)
            if msg.reply:
                message.reply_to = msg.reply
        except Exception:  # noqa: BLE001
            self._dropped_count += 1
            self.logger.exception("Failed to decode inbox message")
            return
        if not message.thread_id:
            message.thread_id = f"thread_{(message.trace_id or message.message_id or new_ulid()).lower()}"

        schema_error = self._validate_schema_version(message)
        if schema_error is not None:
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code=schema_error,
                detail="message schema_version is not supported",
            )
            await self._maybe_reply_error(message, schema_error, "message schema_version is not supported")
            return

        now = time.monotonic()
        self._cleanup_tracking_state(now)

        if self.dev_auth_enabled:
            auth_error = await self._validate_message_auth(message, now=now)
            if auth_error is not None:
                self._dropped_count += 1
                await self._emit_delivery_receipt(
                    message=message,
                    status="rejected",
                    code=auth_error,
                    detail="message signature verification failed",
                )
                await self._maybe_reply_error(message, auth_error, "message signature verification failed")
                return

        if not self._accepting_messages:
            self._busy_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code="shutting_down",
                detail="agent is shutting down",
            )
            await self._maybe_reply_error(message, "shutting_down", "agent is shutting down")
            return

        if self._is_circuit_open(now):
            self._busy_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code="service_degraded",
                detail="circuit breaker open",
            )
            await self._maybe_reply_error(message, "service_degraded", "circuit breaker open")
            return

        ttl_error = self._validate_ttl(message)
        if ttl_error is not None:
            self._expired_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code=ttl_error,
                detail="message ttl invalid or expired",
            )
            await self._maybe_reply_error(message, ttl_error, "message ttl invalid or expired")
            return

        sender_key = message.from_session_tag or message.from_account_id or message.from_agent or "unknown"
        if not self._allow_sender(sender_key, now):
            self._rate_limited_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code="rate_limited",
                detail="sender rate exceeded",
            )
            await self._maybe_reply_error(message, "rate_limited", "sender rate exceeded")
            return

        if message.message_id and message.message_id in self._seen_message_ids:
            self._duplicate_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code="duplicate",
                detail="message_id has already been processed",
            )
            await self._maybe_reply_error(message, "duplicate", "message_id has already been processed")
            return

        idempotency_scope = self._idempotency_scope_key(message)
        if idempotency_scope and idempotency_scope in self._seen_idempotency_keys:
            self._duplicate_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code="duplicate",
                detail="idempotency_key has already been processed",
            )
            await self._maybe_reply_error(message, "duplicate", "idempotency_key has already been processed")
            return

        queue = self._incoming_queue
        if queue is None or queue.full():
            self._busy_count += 1
            self._dropped_count += 1
            await self._emit_delivery_receipt(
                message=message,
                status="rejected",
                code="busy",
                detail="agent backlog is full",
            )
            await self._maybe_reply_error(message, "busy", "agent backlog is full")
            return

        if message.message_id:
            self._seen_message_ids[message.message_id] = now + self.dedupe_ttl_seconds
        if idempotency_scope:
            self._seen_idempotency_keys[idempotency_scope] = now + self.dedupe_ttl_seconds
        queue.put_nowait(message)
        await self._emit_delivery_receipt(message=message, status="accepted")

    async def _worker_loop(self, worker_id: int) -> None:
        queue = self._incoming_queue
        if queue is None:
            return

        while True:
            message = await queue.get()
            self._inflight_count += 1
            started = time.perf_counter()
            trace_id = message.trace_id or message.message_id

            try:
                if self._is_circuit_open():
                    await self._emit_delivery_receipt(
                        message=message,
                        status="rejected",
                        code="service_degraded",
                        detail="circuit breaker open",
                    )
                    await self._maybe_reply_error(message, "service_degraded", "circuit breaker open")
                    continue
                await asyncio.wait_for(self._message_handler(message), timeout=self.work_timeout_seconds)
                self._processed_count += 1
                self._record_success()
                await self._emit_delivery_receipt(message=message, status="processed")
            except asyncio.TimeoutError:
                self._timeout_count += 1
                self._error_count += 1
                self._record_failure()
                await self._emit_delivery_receipt(
                    message=message,
                    status="rejected",
                    code="timeout",
                    detail="handler timed out",
                )
                await self._maybe_reply_error(message, "timeout", "handler timed out")
                self.logger.warning(
                    "message timeout agent=%s session=%s worker=%s message_id=%s trace_id=%s",
                    self.agent_id,
                    self.session_tag,
                    worker_id,
                    message.message_id,
                    trace_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self._error_count += 1
                self._record_failure()
                await self._emit_delivery_receipt(
                    message=message,
                    status="rejected",
                    code="handler_error",
                    detail="handler raised an exception",
                )
                await self._maybe_reply_error(message, "handler_error", "handler raised an exception")
                self.logger.exception(
                    "message handler error agent=%s session=%s worker=%s message_id=%s trace_id=%s",
                    self.agent_id,
                    self.session_tag,
                    worker_id,
                    message.message_id,
                    trace_id,
                )
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._handle_time_total_ms += elapsed_ms
                self._inflight_count -= 1
                queue.task_done()

    def _start_workers(self) -> None:
        self._incoming_queue = asyncio.Queue(maxsize=self.max_pending)
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(i), name=f"agentnet-worker-{self.agent_id}-{i}")
            for i in range(self.max_concurrency)
        ]

    async def _stop_workers(self) -> None:
        queue = self._incoming_queue
        if queue is not None:
            try:
                await asyncio.wait_for(queue.join(), timeout=self.drain_timeout_seconds)
            except asyncio.TimeoutError:
                self.logger.warning(
                    "Drain timeout agent=%s session=%s inflight=%s pending=%s",
                    self.agent_id,
                    self.session_tag,
                    self._inflight_count,
                    queue.qsize(),
                )

        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)

        self._worker_tasks = []
        self._incoming_queue = None
        self._inflight_count = 0

    def _allow_sender(self, sender_key: str, now: float) -> bool:
        tokens, last = self._sender_buckets.get(sender_key, (float(self.rate_limit_burst), now))
        tokens = min(float(self.rate_limit_burst), tokens + (now - last) * self.rate_limit_per_sender_per_sec)
        if tokens < 1.0:
            self._sender_buckets[sender_key] = (tokens, now)
            return False
        self._sender_buckets[sender_key] = (tokens - 1.0, now)
        return True

    def _validate_ttl(self, message: AgentMessage) -> str | None:
        now = datetime.now(UTC)

        expires_at_dt = self._parse_iso_utc(message.expires_at)
        ttl_expires_dt: datetime | None = None
        if message.ttl_ms is not None:
            sent_at_dt = self._parse_iso_utc(message.sent_at)
            if sent_at_dt is not None:
                ttl_expires_dt = sent_at_dt + timedelta(milliseconds=max(0, message.ttl_ms))

        effective_expiry = self._min_datetime(expires_at_dt, ttl_expires_dt)
        if effective_expiry is None:
            return "missing_ttl"
        if now >= effective_expiry:
            return "expired"
        return None

    def _validate_schema_version(self, message: AgentMessage) -> str | None:
        raw_version = str(message.schema_version or "").strip() or "1.0"
        major_raw = raw_version.split(".", 1)[0]
        try:
            major = int(major_raw)
        except (TypeError, ValueError):
            return "schema_version_invalid"
        if major != self.supported_schema_major:
            return "unsupported_schema_version"
        message.schema_version = raw_version
        return None

    def _idempotency_scope_key(self, message: AgentMessage) -> str | None:
        key = str(message.idempotency_key or "").strip()
        if not key:
            return None
        sender = str(message.from_account_id or "").strip() or str(message.from_session_tag or "").strip() or str(message.from_agent or "").strip()
        receiver = str(self.account_id or "").strip() or self.agent_id
        return f"{sender}|{receiver}|{key}"

    @staticmethod
    def _min_datetime(first: datetime | None, second: datetime | None) -> datetime | None:
        if first is None:
            return second
        if second is None:
            return first
        return first if first <= second else second

    @staticmethod
    def _parse_iso_utc(value: str | None) -> datetime | None:
        if not value:
            return None
        raw = value
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def _build_outbound_message(
        self,
        to: str,
        payload: Any,
        kind: str,
        ttl_ms: int | None,
        trace_id: str | None,
        to_account_id: str | None = None,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentMessage:
        message_id = new_id()
        sent_at = utc_now_iso()
        resolved_trace_id = trace_id or message_id
        resolved_thread_id = thread_id or f"thread_{resolved_trace_id.lower()}"
        effective_ttl = self.default_ttl_ms
        if ttl_ms is not None:
            try:
                effective_ttl = max(1, int(ttl_ms))
            except (TypeError, ValueError):
                effective_ttl = self.default_ttl_ms
        expires_at = self._build_expiry_from_sent(sent_at, effective_ttl)
        normalized_idempotency_key = str(idempotency_key or "").strip() or None
        return AgentMessage(
            message_id=message_id,
            from_agent=self.agent_id,
            to_agent=to,
            payload=payload,
            sent_at=sent_at,
            from_account_id=self._require_account_id(),
            to_account_id=to_account_id,
            from_session_tag=self._require_session_tag(),
            ttl_ms=effective_ttl,
            expires_at=expires_at,
            trace_id=resolved_trace_id,
            thread_id=resolved_thread_id,
            parent_message_id=parent_message_id,
            kind=kind,
            schema_version=self.default_schema_version,
            idempotency_key=normalized_idempotency_key,
            auth=self._build_message_auth(
                message_id=message_id,
                to_agent=to,
                payload=payload,
                sent_at=sent_at,
                ttl_ms=effective_ttl,
                expires_at=expires_at,
                trace_id=resolved_trace_id,
                thread_id=resolved_thread_id,
                parent_message_id=parent_message_id,
                kind=kind,
                to_account_id=to_account_id,
                schema_version=self.default_schema_version,
                idempotency_key=normalized_idempotency_key,
            ),
        )

    def _build_expiry_from_sent(self, sent_at: str, ttl_ms: int) -> str:
        sent_dt = self._parse_iso_utc(sent_at) or datetime.now(UTC)
        expires_dt = sent_dt + timedelta(milliseconds=max(1, ttl_ms))
        return expires_dt.isoformat().replace("+00:00", "Z")

    def _encode_message(self, envelope: AgentMessage) -> bytes:
        raw = encode_json(envelope.to_dict())
        if len(raw) > self.max_payload_bytes:
            raise ValueError(f"message exceeds max_payload_bytes={self.max_payload_bytes}")
        return raw

    async def _maybe_reply_error(self, request: AgentMessage, code: str, detail: str) -> None:
        nc = self._nc
        if not request.reply_to or not nc or not nc.is_connected:
            return

        sent_at = utc_now_iso()
        error_reply = AgentMessage(
            message_id=new_id(),
            from_agent=self.agent_id,
            to_agent=request.from_agent,
            payload={
                "error": code,
                "detail": detail,
                "request_message_id": request.message_id,
                "trace_id": request.trace_id or request.message_id,
            },
            sent_at=sent_at,
            from_account_id=self.account_id,
            to_account_id=request.from_account_id,
            from_session_tag=self.session_tag,
            ttl_ms=self.default_ttl_ms,
            expires_at=self._build_expiry_from_sent(sent_at, self.default_ttl_ms),
            trace_id=request.trace_id or request.message_id,
            thread_id=request.thread_id or f"thread_{(request.trace_id or request.message_id or new_ulid()).lower()}",
            parent_message_id=request.message_id or None,
            kind="error",
            schema_version=self.default_schema_version,
        )
        if self.dev_auth_enabled:
            error_reply.auth = self._build_message_auth(
                message_id=error_reply.message_id,
                to_agent=error_reply.to_agent,
                payload=error_reply.payload,
                sent_at=error_reply.sent_at,
                ttl_ms=error_reply.ttl_ms,
                expires_at=error_reply.expires_at,
                trace_id=error_reply.trace_id,
                thread_id=error_reply.thread_id,
                parent_message_id=error_reply.parent_message_id,
                kind=error_reply.kind,
                to_account_id=error_reply.to_account_id,
                schema_version=error_reply.schema_version,
                idempotency_key=error_reply.idempotency_key,
            )
        try:
            await nc.publish(request.reply_to, self._encode_message(error_reply))
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed replying with error code=%s", code)

    def _cleanup_tracking_state(self, now: float) -> None:
        seen_cutoff_keys = [key for key, expiry in self._seen_message_ids.items() if expiry <= now]
        for key in seen_cutoff_keys:
            self._seen_message_ids.pop(key, None)

        idempotency_cutoff_keys = [key for key, expiry in self._seen_idempotency_keys.items() if expiry <= now]
        for key in idempotency_cutoff_keys:
            self._seen_idempotency_keys.pop(key, None)

        bucket_cutoff = now - self.dedupe_ttl_seconds
        stale_sender_keys = [key for key, (_, ts) in self._sender_buckets.items() if ts <= bucket_cutoff]
        for key in stale_sender_keys:
            self._sender_buckets.pop(key, None)

        stale_key_cache = [key for key, (_, expiry) in self._dev_public_key_cache.items() if expiry <= now]
        for key in stale_key_cache:
            self._dev_public_key_cache.pop(key, None)

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures < self.circuit_breaker_failures:
            return
        self._circuit_open_until = time.monotonic() + self.circuit_breaker_reset_seconds
        self._consecutive_failures = 0
        self.logger.error(
            "Circuit opened agent=%s session=%s reset_in_s=%s",
            self.agent_id,
            self.session_tag,
            self.circuit_breaker_reset_seconds,
        )

    def _is_circuit_open(self, now: float | None = None) -> bool:
        check = now if now is not None else time.monotonic()
        return check < self._circuit_open_until

    async def _publish_hello(self, client: NATS | None = None) -> None:
        nc = client or self._nc
        if not nc or not self.session_tag:
            return
        await nc.publish(REGISTRY_HELLO_SUBJECT, encode_json(self.info.to_dict()))

    async def _publish_goodbye(self, client: NATS | None = None) -> None:
        nc = client or self._nc
        if not nc:
            return
        session_tag = self.session_tag
        if not session_tag:
            return
        await nc.publish(
            REGISTRY_GOODBYE_SUBJECT,
            encode_json({"agent_id": self.agent_id, "session_tag": session_tag, "seen_at": utc_now_iso()}),
        )

    async def _register(self, client: NATS | None = None, timeout: float = 5.0) -> None:
        nc = client or self._nc
        if not nc:
            raise RuntimeError("AgentNode is not connected. Call start() first.")
        register_payload = self.info.to_dict()
        if self.dev_auth_enabled:
            private_key, public_key = self._require_dev_auth_keys()
            claims = build_register_claims(
                agent_id=self.agent_id,
                name=self.name,
                username=self.username,
                account_id=self.account_id,
            )
            signature = sign_claims(private_key_b64=private_key, claims=claims)
            register_payload["auth"] = {
                "scheme": "dev-ed25519-v1",
                "public_key": public_key,
                "claims": claims,
                "signature": signature,
            }

        response = await nc.request(REGISTRY_REGISTER_SUBJECT, encode_json(register_payload), timeout=timeout)
        payload = decode_json(response.data)
        if not isinstance(payload, dict):
            raise RuntimeError("Registry register response payload must be a JSON object.")
        if "error" in payload:
            raise RuntimeError(f"Registry register failed: {payload['error']}")

        session_tag = str(payload.get("session_tag") or "")
        if not session_tag:
            raise RuntimeError("Registry register did not return a session_tag.")
        account_id = str(payload.get("account_id") or "")
        if not account_id:
            raise RuntimeError("Registry register did not return an account_id.")
        self.session_tag = session_tag
        username = str(payload.get("username") or "")
        self.account_id = account_id
        if username:
            self.username = username

        heartbeat_interval = payload.get("heartbeat_interval")
        try:
            if heartbeat_interval is not None:
                self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        except (TypeError, ValueError):
            pass

    def _require_session_tag(self) -> str:
        if not self.session_tag:
            raise RuntimeError("Agent session is not registered. Call start() first.")
        return self.session_tag

    def _require_connected_client(self) -> NATS:
        if not self._nc or not self._nc.is_connected:
            raise RuntimeError("AgentNode is not connected. Call start() first.")
        return self._nc

    def _require_account_id(self) -> str:
        if not self.account_id:
            raise RuntimeError("Agent account is not registered. Call start() first.")
        return self.account_id

    def _ensure_dev_auth_identity(self) -> None:
        if not self.dev_auth_enabled:
            return
        if self._dev_private_key and self._dev_public_key:
            return
        key_name = (self.username or self.agent_id).strip() or self.agent_id
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in key_name).strip("._-")
        if not safe:
            safe = self.agent_id
        key_path = Path(self.dev_auth_key_dir) / f"{safe}.json"
        private_key, public_key = load_or_create_dev_keypair(key_path)
        self._dev_private_key = private_key
        self._dev_public_key = public_key

    def _require_dev_auth_keys(self) -> tuple[str, str]:
        if not self.dev_auth_enabled:
            raise RuntimeError("Dev auth is not enabled.")
        self._ensure_dev_auth_identity()
        if not self._dev_private_key or not self._dev_public_key:
            raise RuntimeError("Dev auth keypair is not initialized.")
        return self._dev_private_key, self._dev_public_key

    def _build_message_auth(
        self,
        *,
        message_id: str,
        to_agent: str,
        payload: Any,
        sent_at: str,
        ttl_ms: int | None,
        expires_at: str | None,
        trace_id: str | None,
        thread_id: str | None,
        parent_message_id: str | None,
        kind: str,
        to_account_id: str | None,
        schema_version: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        if not self.dev_auth_enabled:
            return None
        private_key, public_key = self._require_dev_auth_keys()
        from_account_id = self._require_account_id()
        claims = build_message_claims(
            message_id=message_id,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            to_agent=to_agent,
            sent_at=sent_at,
            ttl_ms=ttl_ms,
            expires_at=expires_at,
            trace_id=trace_id,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            kind=kind,
            schema_version=schema_version,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        signature = sign_claims(private_key_b64=private_key, claims=claims)
        return {
            "scheme": DEV_AUTH_SCHEME,
            "public_key": public_key,
            "claims": claims,
            "signature": signature,
        }

    async def _validate_message_auth(self, message: AgentMessage, now: float) -> str | None:
        from_account_id = str(message.from_account_id or "").strip()
        if not from_account_id:
            return "auth_missing_from_account"
        auth = message.auth
        if not isinstance(auth, dict):
            return "auth_missing"
        scheme = str(auth.get("scheme") or "")
        if scheme != DEV_AUTH_SCHEME:
            return "auth_scheme_invalid"
        public_key = str(auth.get("public_key") or "").strip()
        signature = str(auth.get("signature") or "").strip()
        claims = auth.get("claims")
        if not public_key or not signature or not isinstance(claims, dict):
            return "auth_payload_invalid"
        expected_key = await self._resolve_dev_public_key_for_account(from_account_id, now=now)
        if not expected_key:
            return "auth_key_not_found"
        if public_key != expected_key:
            return "auth_public_key_mismatch"

        expected_claims = build_message_claims(
            message_id=message.message_id,
            from_account_id=from_account_id,
            to_account_id=message.to_account_id,
            to_agent=message.to_agent,
            sent_at=message.sent_at,
            ttl_ms=message.ttl_ms,
            expires_at=message.expires_at,
            trace_id=message.trace_id,
            thread_id=message.thread_id,
            parent_message_id=message.parent_message_id,
            kind=message.kind,
            schema_version=message.schema_version,
            idempotency_key=message.idempotency_key,
            payload=message.payload,
        )
        claims_str = {str(k): str(v) for k, v in claims.items()}
        if claims_str != expected_claims:
            legacy_expected_claims = dict(expected_claims)
            legacy_expected_claims.pop("schema_version", None)
            legacy_expected_claims.pop("idempotency_key", None)
            if claims_str != legacy_expected_claims:
                return "auth_claims_mismatch"
        if not verify_claims(public_key_b64=public_key, claims=claims_str, signature_b64=signature):
            return "auth_signature_invalid"
        return None

    async def _resolve_dev_public_key_for_account(self, account_id: str, now: float) -> str | None:
        cached = self._dev_public_key_cache.get(account_id)
        if cached and cached[1] > now:
            return cached[0]
        try:
            if self._nc and self._nc.is_connected:
                key = await resolve_dev_public_key_by_account_with_client(self._nc, account_id=account_id, timeout=2.0)
            else:
                key = await resolve_dev_public_key_by_account(self.nats_url, account_id=account_id, timeout=2.0)
        except Exception:  # noqa: BLE001
            return None
        self._dev_public_key_cache[account_id] = (key, now + self.dev_auth_key_cache_seconds)
        return key

    @staticmethod
    def _parse_account_target(value: str) -> str | None:
        target = value.strip()
        if not target:
            return None
        lowered = target.lower()
        if lowered.startswith("account:"):
            parsed = target.split(":", 1)[1].strip()
            return parsed or None
        if target.startswith("acct_"):
            return target
        return None

    @staticmethod
    def _parse_username_target(value: str) -> str | None:
        target = value.strip()
        if not target:
            return None
        lowered = target.lower()
        if lowered.startswith("capability:"):
            return None
        if lowered.startswith("username:"):
            parsed = target.split(":", 1)[1].strip().lstrip("@")
            return parsed or None
        if target.startswith("@"):
            return target[1:].strip() or None
        return target

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self._publish_hello()
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed publishing registry heartbeat")

    async def __aenter__(self) -> AgentNode:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()
