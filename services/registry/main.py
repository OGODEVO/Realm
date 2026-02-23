"""Registry service that tracks online agents via NATS subjects."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
    REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
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


USERNAME_ALLOWED = re.compile(r"[^a-z0-9._-]+")


def normalize_username(raw: str) -> str:
    normalized = USERNAME_ALLOWED.sub("_", raw.strip().lower())
    normalized = normalized.strip("._-")
    if not normalized:
        return ""
    if len(normalized) > 32:
        normalized = normalized[:32].rstrip("._-")
    return normalized


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
            CREATE TABLE IF NOT EXISTS agent_accounts (
                account_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                visibility TEXT NOT NULL DEFAULT 'public',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_accounts_username ON agent_accounts(username)")

        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_tag TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                account_id TEXT NULL,
                username TEXT NULL,
                server_id TEXT NOT NULL,
                connected_at TIMESTAMPTZ NOT NULL,
                disconnected_at TIMESTAMPTZ NULL,
                last_seen TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        await self._pool.execute("ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS account_id TEXT NULL")
        await self._pool.execute("ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS username TEXT NULL")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent_id ON agent_sessions(agent_id)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_account_id ON agent_sessions(account_id)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_last_seen ON agent_sessions(last_seen)")

    async def resolve_or_create_account(
        self,
        *,
        requested_account_id: str | None,
        requested_username: str | None,
        agent: AgentInfo,
        now: datetime,
    ) -> tuple[str, str]:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")

        async with self._pool.acquire() as conn, conn.transaction():
            desired_username = normalize_username(requested_username or "")
            if not desired_username:
                desired_username = normalize_username(agent.agent_id) or f"agent_{new_ulid()[:8].lower()}"

            if requested_account_id:
                row = await conn.fetchrow(
                    "SELECT account_id, username FROM agent_accounts WHERE account_id = $1",
                    requested_account_id,
                )
                if row:
                    account_id = str(row["account_id"])
                    username = str(row["username"])
                    if desired_username != username:
                        conflict = await conn.fetchval(
                            "SELECT account_id FROM agent_accounts WHERE username = $1",
                            desired_username,
                        )
                        if conflict is None or str(conflict) == account_id:
                            username = desired_username
                    await self._upsert_account_profile(
                        conn=conn,
                        account_id=account_id,
                        username=username,
                        agent=agent,
                        now=now,
                    )
                    return account_id, username

                available_username = await self._next_available_username(conn, desired_username)
                await self._upsert_account_profile(
                    conn=conn,
                    account_id=requested_account_id,
                    username=available_username,
                    agent=agent,
                    now=now,
                )
                return requested_account_id, available_username

            existing_by_username = await conn.fetchrow(
                "SELECT account_id, username FROM agent_accounts WHERE username = $1",
                desired_username,
            )
            if existing_by_username:
                account_id = str(existing_by_username["account_id"])
                username = str(existing_by_username["username"])
                await self._upsert_account_profile(
                    conn=conn,
                    account_id=account_id,
                    username=username,
                    agent=agent,
                    now=now,
                )
                return account_id, username

            account_id = f"acct_{new_ulid().lower()}"
            await self._upsert_account_profile(
                conn=conn,
                account_id=account_id,
                username=desired_username,
                agent=agent,
                now=now,
            )
            return account_id, desired_username

    async def resolve_account_by_username(self, username: str) -> tuple[str, str] | None:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        normalized = normalize_username(username)
        if not normalized:
            return None
        row = await self._pool.fetchrow(
            "SELECT account_id, username FROM agent_accounts WHERE username = $1",
            normalized,
        )
        if not row:
            return None
        return str(row["account_id"]), str(row["username"])

    async def _next_available_username(self, conn: Any, base: str) -> str:
        taken = await conn.fetchval("SELECT account_id FROM agent_accounts WHERE username = $1", base)
        if not taken:
            return base
        for _ in range(10):
            candidate = f"{base[:24]}-{new_ulid()[:6].lower()}".strip("-")
            exists = await conn.fetchval("SELECT account_id FROM agent_accounts WHERE username = $1", candidate)
            if not exists:
                return candidate
        return f"{base[:20]}-{new_ulid()[:10].lower()}".strip("-")

    async def _upsert_account_profile(
        self,
        *,
        conn: Any,
        account_id: str,
        username: str,
        agent: AgentInfo,
        now: datetime,
    ) -> None:
        profile_metadata = {
            "agent_id": agent.agent_id,
            "metadata": agent.metadata,
        }
        await conn.execute(
            """
            INSERT INTO agent_accounts (
                account_id, username, display_name, bio, capabilities, metadata, visibility, status, created_at, updated_at
            )
            VALUES ($1, $2, $3, '', $4::jsonb, $5::jsonb, 'public', 'active', $6, $6)
            ON CONFLICT (account_id) DO UPDATE
            SET
                username = EXCLUDED.username,
                display_name = EXCLUDED.display_name,
                capabilities = EXCLUDED.capabilities,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            """,
            account_id,
            username,
            agent.name,
            json.dumps(agent.capabilities),
            json.dumps(profile_metadata),
            now,
        )

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
                session_tag, agent_id, account_id, username, server_id, connected_at, disconnected_at, last_seen, status, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, 'online', $8::jsonb)
            ON CONFLICT (session_tag) DO UPDATE
            SET
                agent_id = EXCLUDED.agent_id,
                account_id = EXCLUDED.account_id,
                username = EXCLUDED.username,
                server_id = EXCLUDED.server_id,
                disconnected_at = NULL,
                last_seen = EXCLUDED.last_seen,
                status = 'online',
                metadata = EXCLUDED.metadata
            """,
            session_tag,
            agent.agent_id,
            agent.account_id,
            agent.username,
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
        self._local_accounts_by_id: dict[str, dict[str, Any]] = {}
        self._local_account_id_by_username: dict[str, str] = {}
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
        await self._nc.subscribe(REGISTRY_RESOLVE_ACCOUNT_SUBJECT, cb=self._on_resolve_account)

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
        try:
            account_id, username = await self._resolve_or_create_account(agent=agent, data=data, now=now_dt)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed resolving account identity during register")
            await self._nc.publish(msg.reply, encode_json({"error": "failed resolving account identity"}))
            return

        agent.account_id = account_id
        agent.username = username
        agent.session_tag = session_tag
        agent.last_seen = now_iso

        self._sessions[session_tag] = agent
        self._last_seen_by_session[session_tag] = time.monotonic()
        await self._persist_online(session_tag, agent, connected_at=now_dt, last_seen=now_dt)

        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "account_id": account_id,
                    "username": username,
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
            if not incoming.account_id or not incoming.username:
                try:
                    account_id, username = await self._resolve_or_create_account(agent=incoming, data=data, now=now_dt)
                    incoming.account_id = account_id
                    incoming.username = username
                except Exception:  # noqa: BLE001
                    self.logger.exception("Failed resolving account identity during heartbeat restore")
            incoming.session_tag = session_tag
            incoming.last_seen = now_iso
            self._sessions[session_tag] = incoming
            self._last_seen_by_session[session_tag] = time.monotonic()
            await self._persist_online(session_tag, incoming, connected_at=now_dt, last_seen=now_dt)
            return

        if not incoming.account_id:
            incoming.account_id = existing.account_id
        if not incoming.username:
            incoming.username = existing.username
        existing.name = incoming.name or existing.name
        existing.account_id = incoming.account_id
        existing.username = incoming.username
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

    async def _on_resolve_account(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "resolve payload must be an object"}))
            return
        raw_username = str(data.get("username") or "")
        username = normalize_username(raw_username)
        if not username:
            await self._nc.publish(msg.reply, encode_json({"error": "username is required"}))
            return
        try:
            resolved = await self._resolve_account_by_username(username)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed resolving account by username")
            await self._nc.publish(msg.reply, encode_json({"error": "resolve_failed"}))
            return
        if resolved is None:
            await self._nc.publish(msg.reply, encode_json({"error": "not_found"}))
            return
        account_id, resolved_username = resolved
        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "account_id": account_id,
                    "username": resolved_username,
                    "resolved_at": utc_now_iso(),
                }
            ),
        )

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

    async def _resolve_or_create_account(
        self,
        *,
        agent: AgentInfo,
        data: dict[str, Any],
        now: datetime,
    ) -> tuple[str, str]:
        requested_account_id = str(data.get("account_id") or agent.account_id or "") or None
        requested_username = str(data.get("username") or agent.username or "") or None
        if self._store is not None:
            return await self._store.resolve_or_create_account(
                requested_account_id=requested_account_id,
                requested_username=requested_username,
                agent=agent,
                now=now,
            )
        return self._resolve_or_create_account_local(
            requested_account_id=requested_account_id,
            requested_username=requested_username,
            agent=agent,
        )

    def _resolve_or_create_account_local(
        self,
        *,
        requested_account_id: str | None,
        requested_username: str | None,
        agent: AgentInfo,
    ) -> tuple[str, str]:
        desired_username = normalize_username(requested_username or "")
        if not desired_username:
            desired_username = normalize_username(agent.agent_id) or f"agent_{new_ulid()[:8].lower()}"

        if requested_account_id:
            existing = self._local_accounts_by_id.get(requested_account_id)
            if existing:
                username = str(existing.get("username") or desired_username)
                existing.update({"username": username, "agent_id": agent.agent_id, "display_name": agent.name})
                self._local_account_id_by_username[username] = requested_account_id
                return requested_account_id, username

            username = desired_username
            if username in self._local_account_id_by_username:
                username = f"{username[:24]}-{new_ulid()[:6].lower()}".strip("-")
            self._local_accounts_by_id[requested_account_id] = {
                "username": username,
                "agent_id": agent.agent_id,
                "display_name": agent.name,
            }
            self._local_account_id_by_username[username] = requested_account_id
            return requested_account_id, username

        existing_account_id = self._local_account_id_by_username.get(desired_username)
        if existing_account_id:
            return existing_account_id, desired_username

        account_id = f"acct_{new_ulid().lower()}"
        self._local_accounts_by_id[account_id] = {
            "username": desired_username,
            "agent_id": agent.agent_id,
            "display_name": agent.name,
        }
        self._local_account_id_by_username[desired_username] = account_id
        return account_id, desired_username

    async def _resolve_account_by_username(self, username: str) -> tuple[str, str] | None:
        normalized = normalize_username(username)
        if not normalized:
            return None
        if self._store is not None:
            return await self._store.resolve_account_by_username(normalized)
        account_id = self._local_account_id_by_username.get(normalized)
        if not account_id:
            return None
        account = self._local_accounts_by_id.get(account_id) or {}
        resolved_username = str(account.get("username") or normalized)
        return account_id, resolved_username

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
