"""Registry service that tracks online agents via NATS subjects."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from agentnet.config import DEFAULT_REGISTRY_SERVICE_NATS_URL
from agentnet.schema import AgentInfo
from agentnet.subjects import (
    REGISTRY_GOODBYE_SUBJECT,
    REGISTRY_HELLO_SUBJECT,
    REGISTRY_LIST_SUBJECT,
    REGISTRY_REGISTER_SUBJECT,
)
from agentnet.utils import decode_json, encode_json, new_ulid, utc_now_iso

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - optional when DB is disabled
    asyncpg = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PostgresSessionStore:
    def __init__(self, database_url: str, retention_days: float = 14.0, logger: logging.Logger | None = None) -> None:
        self.database_url = database_url
        self.retention_days = max(1.0, retention_days)
        self.logger = logger or logging.getLogger("agentnet.registry.db")
        self._pool: Any | None = None

    async def start(self) -> None:
        if asyncpg is None:
            raise RuntimeError("DATABASE_URL provided, but asyncpg is not installed.")
        self._pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5, command_timeout=10.0)
        await self._ensure_schema()

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_schema(self) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_tag TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                connected_at TIMESTAMPTZ NOT NULL,
                disconnected_at TIMESTAMPTZ NULL,
                last_seen TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent_id ON agent_sessions(agent_id)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_last_seen ON agent_sessions(last_seen)")

    async def upsert_online(
        self,
        session_tag: str,
        agent: AgentInfo,
        server_id: str,
        connected_at: datetime,
        last_seen: datetime,
    ) -> None:
        if self._pool is None:
            return
        metadata_payload = {
            "name": agent.name,
            "capabilities": agent.capabilities,
            "metadata": agent.metadata,
        }
        await self._pool.execute(
            """
            INSERT INTO agent_sessions (
                session_tag, agent_id, server_id, connected_at, disconnected_at, last_seen, status, metadata
            )
            VALUES ($1, $2, $3, $4, NULL, $5, 'online', $6::jsonb)
            ON CONFLICT (session_tag) DO UPDATE
            SET
                agent_id = EXCLUDED.agent_id,
                server_id = EXCLUDED.server_id,
                disconnected_at = NULL,
                last_seen = EXCLUDED.last_seen,
                status = 'online',
                metadata = EXCLUDED.metadata
            """,
            session_tag,
            agent.agent_id,
            server_id,
            connected_at,
            last_seen,
            json.dumps(metadata_payload),
        )

    async def mark_offline(self, session_tag: str, disconnected_at: datetime) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            """
            UPDATE agent_sessions
            SET disconnected_at = $2, last_seen = $2, status = 'offline'
            WHERE session_tag = $1
            """,
            session_tag,
            disconnected_at,
        )

    async def prune_offline(self, now: datetime) -> None:
        if self._pool is None:
            return
        cutoff = now - timedelta(days=self.retention_days)
        await self._pool.execute(
            """
            DELETE FROM agent_sessions
            WHERE status = 'offline'
              AND disconnected_at IS NOT NULL
              AND disconnected_at < $1
            """,
            cutoff,
        )


class RegistryService:
    def __init__(
        self,
        nats_url: str,
        ttl_seconds: float = 40.0,
        gc_interval_seconds: float = 5.0,
        heartbeat_interval_seconds: float = 12.0,
        server_id: str = "registry",
        database_url: str | None = None,
        session_retention_days: float = 14.0,
    ) -> None:
        self.nats_url = nats_url
        self.ttl_seconds = ttl_seconds
        self.gc_interval_seconds = gc_interval_seconds
        self.heartbeat_interval_seconds = max(1.0, heartbeat_interval_seconds)
        self.server_id = server_id
        self.logger = logging.getLogger(f"agentnet.registry.{server_id}")

        self._nc: NATS | None = None
        self._gc_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, AgentInfo] = {}
        self._last_seen_by_session: dict[str, float] = {}
        self._store = (
            PostgresSessionStore(database_url, retention_days=session_retention_days, logger=self.logger)
            if database_url
            else None
        )

    async def start(self) -> None:
        if self._store is not None:
            await self._store.start()

        self._nc = NATS()
        await self._nc.connect(servers=[self.nats_url], name="agentnet-registry")

        await self._nc.subscribe(REGISTRY_REGISTER_SUBJECT, cb=self._on_register)
        await self._nc.subscribe(REGISTRY_HELLO_SUBJECT, cb=self._on_hello)
        await self._nc.subscribe(REGISTRY_GOODBYE_SUBJECT, cb=self._on_goodbye)
        await self._nc.subscribe(REGISTRY_LIST_SUBJECT, cb=self._on_list)

        self._gc_task = asyncio.create_task(self._gc_loop(), name="agentnet-registry-gc")
        print(
            f"registry ready: nats={self.nats_url} server_id={self.server_id} "
            f"ttl={self.ttl_seconds}s heartbeat={self.heartbeat_interval_seconds}s "
            f"db={'enabled' if self._store else 'disabled'}"
        )

    async def stop(self) -> None:
        if self._gc_task:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass
            self._gc_task = None

        if self._nc and self._nc.is_connected:
            await self._nc.drain()
        self._nc = None

        if self._store is not None:
            await self._store.stop()

    async def _on_register(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return

        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "register payload must be an object"}))
            return

        agent = AgentInfo.from_dict(data)
        if not agent.agent_id:
            await self._nc.publish(msg.reply, encode_json({"error": "agent_id is required"}))
            return

        session_tag = f"{self.server_id}_{new_ulid()}"
        now_dt = _utc_now()
        now_iso = _iso_utc(now_dt)
        agent.session_tag = session_tag
        agent.last_seen = now_iso

        self._sessions[session_tag] = agent
        self._last_seen_by_session[session_tag] = time.monotonic()
        await self._persist_online(session_tag, agent, connected_at=now_dt, last_seen=now_dt)

        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "session_tag": session_tag,
                    "heartbeat_interval": self.heartbeat_interval_seconds,
                    "ttl_seconds": self.ttl_seconds,
                    "registered_at": now_iso,
                }
            ),
        )

    async def _on_hello(self, msg: Msg) -> None:
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            return

        session_tag = str(data.get("session_tag") or "")
        if not session_tag:
            return

        incoming = AgentInfo.from_dict(data)
        if not incoming.agent_id:
            return

        now_dt = _utc_now()
        now_iso = _iso_utc(now_dt)
        existing = self._sessions.get(session_tag)
        if existing is None:
            incoming.session_tag = session_tag
            incoming.last_seen = now_iso
            self._sessions[session_tag] = incoming
            self._last_seen_by_session[session_tag] = time.monotonic()
            await self._persist_online(session_tag, incoming, connected_at=now_dt, last_seen=now_dt)
            return

        existing.name = incoming.name or existing.name
        existing.capabilities = incoming.capabilities
        existing.metadata = incoming.metadata
        existing.last_seen = now_iso
        self._last_seen_by_session[session_tag] = time.monotonic()
        await self._persist_online(session_tag, existing, connected_at=now_dt, last_seen=now_dt)

    async def _on_goodbye(self, msg: Msg) -> None:
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            return

        session_tag = str(data.get("session_tag") or "")
        if not session_tag:
            return

        self._sessions.pop(session_tag, None)
        self._last_seen_by_session.pop(session_tag, None)
        await self._persist_offline(session_tag, disconnected_at=_utc_now())

    async def _on_list(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return

        await self._evict_stale()

        payload = {
            "generated_at": utc_now_iso(),
            "agents": [
                agent.to_dict()
                for agent in sorted(
                    self._sessions.values(),
                    key=lambda item: (item.agent_id, item.session_tag or ""),
                )
            ],
        }
        await self._nc.publish(msg.reply, encode_json(payload))

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(self.gc_interval_seconds)
            await self._evict_stale()
            await self._prune_persisted_sessions()

    async def _evict_stale(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        stale_session_tags = [
            session_tag for session_tag, seen in self._last_seen_by_session.items() if seen < cutoff
        ]
        if not stale_session_tags:
            return

        now_dt = _utc_now()
        for session_tag in stale_session_tags:
            self._last_seen_by_session.pop(session_tag, None)
            self._sessions.pop(session_tag, None)
            await self._persist_offline(session_tag, disconnected_at=now_dt)

    async def _persist_online(
        self,
        session_tag: str,
        agent: AgentInfo,
        connected_at: datetime,
        last_seen: datetime,
    ) -> None:
        if self._store is None:
            return
        try:
            await self._store.upsert_online(
                session_tag=session_tag,
                agent=agent,
                server_id=self.server_id,
                connected_at=connected_at,
                last_seen=last_seen,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed persisting online session session_tag=%s", session_tag)

    async def _persist_offline(self, session_tag: str, disconnected_at: datetime) -> None:
        if self._store is None:
            return
        try:
            await self._store.mark_offline(session_tag=session_tag, disconnected_at=disconnected_at)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed persisting offline session session_tag=%s", session_tag)

    async def _prune_persisted_sessions(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.prune_offline(now=_utc_now())
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed pruning offline session history")


async def amain() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    nats_url = os.getenv("NATS_URL", DEFAULT_REGISTRY_SERVICE_NATS_URL)
    ttl_seconds = float(os.getenv("AGENT_TTL_SECONDS", "40"))
    gc_interval = float(os.getenv("GC_INTERVAL_SECONDS", "5"))
    heartbeat_interval = float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "12"))
    server_id = os.getenv("REGISTRY_SERVER_ID", "registry")
    database_url = os.getenv("DATABASE_URL")
    retention_days = float(os.getenv("SESSION_RETENTION_DAYS", "14"))

    service = RegistryService(
        nats_url=nats_url,
        ttl_seconds=ttl_seconds,
        gc_interval_seconds=gc_interval,
        heartbeat_interval_seconds=heartbeat_interval,
        server_id=server_id,
        database_url=database_url,
        session_retention_days=retention_days,
    )
    await service.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await service.stop()


if __name__ == "__main__":
    asyncio.run(amain())
