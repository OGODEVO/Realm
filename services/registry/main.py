"""Registry service that tracks online agents via NATS subjects."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import signal
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from agentnet.config import DEFAULT_REGISTRY_SERVICE_NATS_URL
from agentnet.dev_auth import DEV_AUTH_SCHEME, parse_bool, parse_iso_utc, verify_claims
from agentnet.schema import AgentInfo, AgentMessage
from agentnet.subjects import (
    REGISTRY_GOODBYE_SUBJECT,
    REGISTRY_HELLO_SUBJECT,
    REGISTRY_LIST_SUBJECT,
    REGISTRY_METRICS_SUBJECT,
    REGISTRY_MESSAGE_SEARCH_SUBJECT,
    REGISTRY_PROFILE_SUBJECT,
    REGISTRY_REGISTER_SUBJECT,
    REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
    REGISTRY_RESOLVE_KEY_SUBJECT,
    REGISTRY_SEARCH_SUBJECT,
    REGISTRY_THREAD_LIST_SUBJECT,
    REGISTRY_THREAD_MESSAGES_SUBJECT,
    REGISTRY_THREAD_STATUS_SUBJECT,
    account_inbox_subject,
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


def _coerce_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _estimate_tokens(byte_count: int, chars_per_token: float) -> int:
    if byte_count <= 0:
        return 0
    return int(math.ceil(float(byte_count) / max(1.0, float(chars_per_token))))


def _extract_checkpoint_end(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    payload_type = str(payload.get("type") or "").strip().lower()
    if payload_type != "checkpoint":
        return 0
    return _coerce_non_negative_int(payload.get("covers_end"), default=0)


def _classify_thread_status(approx_tokens: int, soft_limit_tokens: int, hard_limit_tokens: int) -> str:
    soft = max(1, soft_limit_tokens)
    hard = max(soft, hard_limit_tokens)
    if approx_tokens >= hard:
        return "needs_compaction"
    if approx_tokens >= soft:
        return "warn"
    return "ok"


def _encode_cursor(*, sent_at: datetime | str, message_id: str) -> str:
    if isinstance(sent_at, datetime):
        sent_at_value = _iso_utc(sent_at)
    else:
        sent_at_value = str(sent_at or "")
    payload = {"sent_at": sent_at_value, "message_id": str(message_id or "")}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    normalized = str(cursor or "").strip()
    if not normalized:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(normalized.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, None
    if not isinstance(data, dict):
        return None, None
    sent_at = parse_iso_utc(str(data.get("sent_at") or ""))
    message_id = str(data.get("message_id") or "").strip() or None
    if sent_at is None or message_id is None:
        return None, None
    return sent_at, message_id


class PostgresSessionStore:
    def __init__(
        self,
        database_url: str,
        retention_days: float = 14.0,
        token_estimate_chars_per_token: float = 4.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.database_url = database_url
        self.retention_days = max(1.0, retention_days)
        self.token_estimate_chars_per_token = max(1.0, token_estimate_chars_per_token)
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

        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_threads (
                thread_id TEXT PRIMARY KEY,
                created_by_account_id TEXT NULL,
                participants JSONB NOT NULL DEFAULT '[]'::jsonb,
                title TEXT NOT NULL DEFAULT '',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        await self._pool.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS message_count BIGINT NOT NULL DEFAULT 0")
        await self._pool.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS byte_count BIGINT NOT NULL DEFAULT 0")
        await self._pool.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS approx_tokens BIGINT NOT NULL DEFAULT 0")
        await self._pool.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS latest_checkpoint_end BIGINT NOT NULL DEFAULT 0")
        await self._pool.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ NULL")
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                parent_message_id TEXT NULL,
                from_account_id TEXT NULL,
                from_session_tag TEXT NULL,
                to_account_id TEXT NULL,
                to_agent TEXT NOT NULL,
                kind TEXT NOT NULL,
                schema_version TEXT NOT NULL DEFAULT '1.0',
                idempotency_key TEXT NULL,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                trace_id TEXT NULL,
                sent_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'received',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        await self._pool.execute("ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT '1.0'")
        await self._pool.execute("ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS idempotency_key TEXT NULL")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_thread_id ON agent_messages(thread_id)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_sent_at ON agent_messages(sent_at)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_from_account ON agent_messages(from_account_id)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_to_account ON agent_messages(to_account_id)")
        await self._pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_messages_thread_sent_msg ON agent_messages(thread_id, sent_at DESC, message_id DESC)"
        )
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_kind ON agent_messages(kind)")
        await self._pool.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_idempotency_key ON agent_messages(idempotency_key)")

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

    async def get_account_dev_public_key(self, account_id: str) -> str | None:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        raw = await self._pool.fetchval(
            "SELECT metadata ->> 'dev_auth_public_key' FROM agent_accounts WHERE account_id = $1",
            account_id,
        )
        if raw is None:
            return None
        value = str(raw).strip()
        return value or None

    async def bind_account_dev_public_key(self, account_id: str, public_key: str, now: datetime) -> None:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        payload = json.dumps({"dev_auth_public_key": public_key})
        await self._pool.execute(
            """
            UPDATE agent_accounts
            SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb, updated_at = $3
            WHERE account_id = $1
            """,
            account_id,
            payload,
            now,
        )

    async def search_accounts(
        self,
        *,
        query: str,
        capability: str | None,
        limit: int,
        online_only: bool,
    ) -> list[dict[str, Any]]:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        q = query.strip()
        cap = (capability or "").strip()
        safe_limit = max(1, min(int(limit), 100))
        rows = await self._pool.fetch(
            """
            SELECT
                a.account_id,
                a.username,
                a.display_name,
                a.capabilities,
                a.metadata,
                a.visibility,
                a.status,
                COALESCE((
                    SELECT COUNT(*)
                    FROM agent_sessions s
                    WHERE s.account_id = a.account_id AND s.status = 'online'
                ), 0)::INT AS online_sessions
            FROM agent_accounts a
            WHERE
                ($1 = '' OR a.username ILIKE ('%' || $1 || '%') OR a.display_name ILIKE ('%' || $1 || '%'))
                AND ($2 = '' OR a.capabilities ? $2)
                AND (
                    NOT $3 OR EXISTS (
                        SELECT 1 FROM agent_sessions s2
                        WHERE s2.account_id = a.account_id AND s2.status = 'online'
                    )
                )
            ORDER BY online_sessions DESC, a.username ASC
            LIMIT $4
            """,
            q,
            cap,
            bool(online_only),
            safe_limit,
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            online_sessions = int(row["online_sessions"] or 0)
            results.append(
                {
                    "account_id": str(row["account_id"]),
                    "username": str(row["username"]),
                    "display_name": str(row["display_name"] or row["username"]),
                    "capabilities": row["capabilities"] if isinstance(row["capabilities"], list) else [],
                    "metadata": row["metadata"] if isinstance(row["metadata"], dict) else {},
                    "visibility": str(row["visibility"] or "public"),
                    "status": str(row["status"] or "active"),
                    "online_sessions": online_sessions,
                    "online": online_sessions > 0,
                }
            )
        return results

    async def get_account_profile(
        self,
        *,
        account_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, Any] | None:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        if account_id:
            row = await self._pool.fetchrow(
                """
                SELECT account_id, username, display_name, bio, capabilities, metadata, visibility, status, created_at, updated_at
                FROM agent_accounts
                WHERE account_id = $1
                """,
                account_id,
            )
        else:
            normalized = normalize_username(username or "")
            if not normalized:
                return None
            row = await self._pool.fetchrow(
                """
                SELECT account_id, username, display_name, bio, capabilities, metadata, visibility, status, created_at, updated_at
                FROM agent_accounts
                WHERE username = $1
                """,
                normalized,
            )
        if not row:
            return None
        resolved_account_id = str(row["account_id"])
        sessions = await self._pool.fetch(
            """
            SELECT session_tag, agent_id, server_id, connected_at, last_seen, metadata
            FROM agent_sessions
            WHERE account_id = $1 AND status = 'online'
            ORDER BY last_seen DESC
            """,
            resolved_account_id,
        )
        online_sessions: list[dict[str, Any]] = []
        for session in sessions:
            meta = session["metadata"] if isinstance(session["metadata"], dict) else {}
            online_sessions.append(
                {
                    "session_tag": str(session["session_tag"]),
                    "agent_id": str(session["agent_id"]),
                    "server_id": str(session["server_id"]),
                    "connected_at": _iso_utc(session["connected_at"]),
                    "last_seen": _iso_utc(session["last_seen"]),
                    "capabilities": meta.get("capabilities") if isinstance(meta.get("capabilities"), list) else [],
                    "metadata": meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {},
                }
            )

        return {
            "account_id": resolved_account_id,
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or row["username"]),
            "bio": str(row["bio"] or ""),
            "capabilities": row["capabilities"] if isinstance(row["capabilities"], list) else [],
            "metadata": row["metadata"] if isinstance(row["metadata"], dict) else {},
            "visibility": str(row["visibility"] or "public"),
            "status": str(row["status"] or "active"),
            "created_at": _iso_utc(row["created_at"]),
            "updated_at": _iso_utc(row["updated_at"]),
            "online_sessions": online_sessions,
            "online": bool(online_sessions),
        }

    async def get_thread_status(self, thread_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return None
        row = await self._pool.fetchrow(
            """
            SELECT
                thread_id,
                created_by_account_id,
                participants,
                created_at,
                updated_at,
                message_count,
                byte_count,
                approx_tokens,
                latest_checkpoint_end,
                last_message_at
            FROM agent_threads
            WHERE thread_id = $1
            """,
            normalized_thread_id,
        )
        if not row:
            return None
        participants = row["participants"] if isinstance(row["participants"], list) else []
        return {
            "thread_id": str(row["thread_id"]),
            "created_by_account_id": str(row["created_by_account_id"]) if row["created_by_account_id"] is not None else None,
            "participants": [str(item) for item in participants],
            "created_at": _iso_utc(row["created_at"]),
            "updated_at": _iso_utc(row["updated_at"]),
            "message_count": _coerce_non_negative_int(row["message_count"]),
            "byte_count": _coerce_non_negative_int(row["byte_count"]),
            "approx_tokens": _coerce_non_negative_int(row["approx_tokens"]),
            "latest_checkpoint_end": _coerce_non_negative_int(row["latest_checkpoint_end"]),
            "last_message_at": _iso_utc(row["last_message_at"]) if row["last_message_at"] is not None else None,
        }

    async def list_threads(
        self,
        *,
        participant_account_id: str | None,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        safe_limit = max(1, min(int(limit), 100))
        normalized_query = str(query or "").strip()
        participant = str(participant_account_id or "").strip()
        rows = await self._pool.fetch(
            """
            SELECT
                thread_id,
                created_by_account_id,
                participants,
                created_at,
                updated_at,
                message_count,
                byte_count,
                approx_tokens,
                latest_checkpoint_end,
                last_message_at
            FROM agent_threads
            WHERE
                ($1 = '' OR thread_id ILIKE ('%' || $1 || '%'))
                AND ($2 = '' OR participants ? $2)
            ORDER BY COALESCE(last_message_at, updated_at) DESC, thread_id DESC
            LIMIT $3
            """,
            normalized_query,
            participant,
            safe_limit,
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            participants = row["participants"] if isinstance(row["participants"], list) else []
            results.append(
                {
                    "thread_id": str(row["thread_id"]),
                    "created_by_account_id": str(row["created_by_account_id"]) if row["created_by_account_id"] is not None else None,
                    "participants": [str(item) for item in participants],
                    "created_at": _iso_utc(row["created_at"]),
                    "updated_at": _iso_utc(row["updated_at"]),
                    "message_count": _coerce_non_negative_int(row["message_count"]),
                    "byte_count": _coerce_non_negative_int(row["byte_count"]),
                    "approx_tokens": _coerce_non_negative_int(row["approx_tokens"]),
                    "latest_checkpoint_end": _coerce_non_negative_int(row["latest_checkpoint_end"]),
                    "last_message_at": _iso_utc(row["last_message_at"]) if row["last_message_at"] is not None else None,
                }
            )
        return results

    @staticmethod
    def _row_to_message(row: Any) -> dict[str, Any]:
        payload = row["payload"] if isinstance(row["payload"], (dict, list, str, int, float, bool)) or row["payload"] is None else {}
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else {}
        sent_at_value = row["sent_at"]
        received_at_value = row["received_at"]
        return {
            "message_id": str(row["message_id"]),
            "thread_id": str(row["thread_id"]),
            "parent_message_id": str(row["parent_message_id"]) if row["parent_message_id"] is not None else None,
            "from_account_id": str(row["from_account_id"]) if row["from_account_id"] is not None else None,
            "from_session_tag": str(row["from_session_tag"]) if row["from_session_tag"] is not None else None,
            "to_account_id": str(row["to_account_id"]) if row["to_account_id"] is not None else None,
            "to_agent": str(row["to_agent"] or ""),
            "kind": str(row["kind"] or ""),
            "schema_version": str(row["schema_version"] or "1.0"),
            "idempotency_key": str(row["idempotency_key"]) if row["idempotency_key"] is not None else None,
            "payload": payload,
            "trace_id": str(row["trace_id"]) if row["trace_id"] is not None else None,
            "sent_at": _iso_utc(sent_at_value) if isinstance(sent_at_value, datetime) else str(sent_at_value or ""),
            "received_at": _iso_utc(received_at_value) if isinstance(received_at_value, datetime) else str(received_at_value or ""),
            "status": str(row["status"] or ""),
            "metadata": metadata,
        }

    async def list_thread_messages(
        self,
        *,
        thread_id: str,
        limit: int,
        cursor_sent_at: datetime | None,
        cursor_message_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return [], None
        safe_limit = max(1, min(int(limit), 200))
        fetch_limit = safe_limit + 1
        rows = await self._pool.fetch(
            """
            SELECT
                message_id, thread_id, parent_message_id, from_account_id, from_session_tag, to_account_id,
                to_agent, kind, schema_version, idempotency_key, payload, trace_id, sent_at, received_at, status, metadata
            FROM agent_messages
            WHERE
                thread_id = $1
                AND (
                    $2::timestamptz IS NULL
                    OR sent_at < $2
                    OR (sent_at = $2 AND message_id < $3)
                )
            ORDER BY sent_at DESC, message_id DESC
            LIMIT $4
            """,
            normalized_thread_id,
            cursor_sent_at,
            cursor_message_id,
            fetch_limit,
        )
        has_more = len(rows) > safe_limit
        slice_rows = rows[:safe_limit]
        messages = [self._row_to_message(row) for row in slice_rows]
        next_cursor: str | None = None
        if has_more and slice_rows:
            last_row = slice_rows[-1]
            next_cursor = _encode_cursor(sent_at=last_row["sent_at"], message_id=str(last_row["message_id"]))
        return messages, next_cursor

    async def search_messages(
        self,
        *,
        thread_id: str | None,
        from_account_id: str | None,
        to_account_id: str | None,
        kind: str | None,
        from_ts: datetime | None,
        to_ts: datetime | None,
        limit: int,
        cursor_sent_at: datetime | None,
        cursor_message_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._pool is None:
            raise RuntimeError("Postgres session store is not started")
        safe_limit = max(1, min(int(limit), 200))
        fetch_limit = safe_limit + 1
        normalized_thread_id = str(thread_id or "").strip()
        normalized_from_account_id = str(from_account_id or "").strip()
        normalized_to_account_id = str(to_account_id or "").strip()
        normalized_kind = str(kind or "").strip()
        rows = await self._pool.fetch(
            """
            SELECT
                message_id, thread_id, parent_message_id, from_account_id, from_session_tag, to_account_id,
                to_agent, kind, schema_version, idempotency_key, payload, trace_id, sent_at, received_at, status, metadata
            FROM agent_messages
            WHERE
                ($1 = '' OR thread_id = $1)
                AND ($2 = '' OR from_account_id = $2)
                AND ($3 = '' OR to_account_id = $3)
                AND ($4 = '' OR kind = $4)
                AND ($5::timestamptz IS NULL OR sent_at >= $5)
                AND ($6::timestamptz IS NULL OR sent_at <= $6)
                AND (
                    $7::timestamptz IS NULL
                    OR sent_at < $7
                    OR (sent_at = $7 AND message_id < $8)
                )
            ORDER BY sent_at DESC, message_id DESC
            LIMIT $9
            """,
            normalized_thread_id,
            normalized_from_account_id,
            normalized_to_account_id,
            normalized_kind,
            from_ts,
            to_ts,
            cursor_sent_at,
            cursor_message_id,
            fetch_limit,
        )
        has_more = len(rows) > safe_limit
        slice_rows = rows[:safe_limit]
        messages = [self._row_to_message(row) for row in slice_rows]
        next_cursor: str | None = None
        if has_more and slice_rows:
            last_row = slice_rows[-1]
            next_cursor = _encode_cursor(sent_at=last_row["sent_at"], message_id=str(last_row["message_id"]))
        return messages, next_cursor

    async def _persist_message_with_conn(self, conn: Any, message: AgentMessage, received_at: datetime) -> bool:
        message_id = str(message.message_id or "").strip()
        if not message_id:
            return False
        thread_id = str(message.thread_id or "").strip() or f"thread_{(message.trace_id or message_id).lower()}"
        participant_values = [str(v).strip() for v in (message.from_account_id, message.to_account_id) if str(v or "").strip()]
        merged_participants = sorted(set(participant_values))
        sent_at = parse_iso_utc(message.sent_at or "") or received_at
        payload_value = message.payload
        try:
            payload_json = json.dumps(payload_value)
        except TypeError:
            payload_json = json.dumps({"text": str(payload_value)})
        payload_bytes = len(payload_json.encode("utf-8"))
        payload_tokens = _estimate_tokens(payload_bytes, self.token_estimate_chars_per_token)
        checkpoint_end = _extract_checkpoint_end(payload_value)
        metadata_payload = {
            "ttl_ms": message.ttl_ms,
            "expires_at": message.expires_at,
            "auth_scheme": str((message.auth or {}).get("scheme") or ""),
        }
        await conn.execute(
            """
            INSERT INTO agent_threads (thread_id, created_by_account_id, participants, title, metadata, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, '', $4::jsonb, $5, $5)
            ON CONFLICT (thread_id) DO UPDATE
            SET
                participants = (
                    SELECT to_jsonb(ARRAY(
                        SELECT DISTINCT value
                        FROM jsonb_array_elements_text(
                            COALESCE(agent_threads.participants, '[]'::jsonb) || EXCLUDED.participants
                        ) AS t(value)
                    ))
                ),
                updated_at = EXCLUDED.updated_at
            """,
            thread_id,
            message.from_account_id,
            json.dumps(merged_participants),
            json.dumps({}),
            received_at,
        )
        insert_result = await conn.execute(
            """
            INSERT INTO agent_messages (
                message_id, thread_id, parent_message_id, from_account_id, from_session_tag, to_account_id, to_agent,
                kind, schema_version, idempotency_key, payload, trace_id, sent_at, received_at, status, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14, 'received', $15::jsonb)
            ON CONFLICT (message_id) DO NOTHING
            """,
            message_id,
            thread_id,
            message.parent_message_id,
            message.from_account_id,
            message.from_session_tag,
            message.to_account_id,
            message.to_agent,
            message.kind,
            str(message.schema_version or "1.0"),
            str(message.idempotency_key or "") or None,
            payload_json,
            message.trace_id,
            sent_at,
            received_at,
            json.dumps(metadata_payload),
        )
        inserted = insert_result.endswith("1")
        if inserted:
            await conn.execute(
                """
                UPDATE agent_threads
                SET
                    message_count = COALESCE(message_count, 0) + $2,
                    byte_count = COALESCE(byte_count, 0) + $3,
                    approx_tokens = COALESCE(approx_tokens, 0) + $4,
                    latest_checkpoint_end = GREATEST(COALESCE(latest_checkpoint_end, 0), $5),
                    last_message_at = GREATEST(COALESCE(last_message_at, $6), $6),
                    updated_at = GREATEST(updated_at, $6)
                WHERE thread_id = $1
                """,
                thread_id,
                1,
                payload_bytes,
                payload_tokens,
                checkpoint_end,
                sent_at,
            )
        return inserted

    async def persist_message(self, message: AgentMessage, received_at: datetime) -> None:
        if self._pool is None:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await self._persist_message_with_conn(conn, message, received_at)

    async def persist_messages_batch(self, items: list[tuple[AgentMessage, datetime]]) -> None:
        if self._pool is None or not items:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            for message, received_at in items:
                await self._persist_message_with_conn(conn, message, received_at)

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
                metadata = COALESCE(agent_accounts.metadata, '{}'::jsonb) || EXCLUDED.metadata,
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
        thread_soft_limit_tokens: int = 50000,
        thread_hard_limit_tokens: int = 60000,
        thread_keep_tail_messages: int = 24,
        token_estimate_chars_per_token: float = 4.0,
        server_id: str = "registry",
        database_url: str | None = None,
        session_retention_days: float = 14.0,
        dev_auth_enabled: bool = False,
        dev_auth_max_skew_seconds: float = 120.0,
        dev_auth_nonce_ttl_seconds: float = 300.0,
        compaction_event_enabled: bool = True,
        compaction_event_cooldown_seconds: float = 120.0,
        db_write_batch_enabled: bool = True,
        db_write_batch_size: int = 64,
        db_write_flush_ms: float = 40.0,
        db_write_queue_max: int = 10000,
    ) -> None:
        self.nats_url = nats_url
        self.ttl_seconds = ttl_seconds
        self.gc_interval_seconds = gc_interval_seconds
        self.heartbeat_interval_seconds = max(1.0, heartbeat_interval_seconds)
        self.thread_soft_limit_tokens = max(1, int(thread_soft_limit_tokens))
        self.thread_hard_limit_tokens = max(self.thread_soft_limit_tokens, int(thread_hard_limit_tokens))
        self.thread_keep_tail_messages = max(0, int(thread_keep_tail_messages))
        self.token_estimate_chars_per_token = max(1.0, float(token_estimate_chars_per_token))
        self.server_id = server_id
        self.logger = logging.getLogger(f"agentnet.registry.{server_id}")
        self.dev_auth_enabled = bool(dev_auth_enabled)
        self.dev_auth_max_skew_seconds = max(1.0, dev_auth_max_skew_seconds)
        self.dev_auth_nonce_ttl_seconds = max(5.0, dev_auth_nonce_ttl_seconds)
        self.compaction_event_enabled = bool(compaction_event_enabled)
        self.compaction_event_cooldown_seconds = max(5.0, float(compaction_event_cooldown_seconds))
        self.db_write_batch_enabled = bool(db_write_batch_enabled)
        self.db_write_batch_size = max(1, int(db_write_batch_size))
        self.db_write_flush_seconds = max(0.001, float(db_write_flush_ms) / 1000.0)
        self.db_write_queue_max = max(100, int(db_write_queue_max))

        self._nc: NATS | None = None
        self._gc_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, AgentInfo] = {}
        self._last_seen_by_session: dict[str, float] = {}
        self._local_accounts_by_id: dict[str, dict[str, Any]] = {}
        self._local_account_id_by_username: dict[str, str] = {}
        self._local_dev_public_key_by_account: dict[str, str] = {}
        self._local_threads: dict[str, dict[str, Any]] = {}
        self._local_messages: dict[str, dict[str, Any]] = {}
        self._seen_register_nonces: dict[str, float] = {}
        self._compaction_event_state: dict[str, tuple[int, float]] = {}
        self._db_write_queue: asyncio.Queue[tuple[AgentMessage, datetime] | None] | None = None
        self._db_write_worker_task: asyncio.Task[None] | None = None
        self._db_write_queue_overflow_count = 0
        self._metrics_window_size = max(32, int(os.getenv("REGISTRY_METRICS_WINDOW_SIZE", "1024")))
        self._latency_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._metrics_window_size)
        )
        self._handler_calls: dict[str, int] = defaultdict(int)
        self._handler_errors: dict[str, int] = defaultdict(int)
        self._store = (
            PostgresSessionStore(
                database_url,
                retention_days=session_retention_days,
                token_estimate_chars_per_token=self.token_estimate_chars_per_token,
                logger=self.logger,
            )
            if database_url
            else None
        )

    async def start(self) -> None:
        if self._store is not None:
            await self._store.start()
            if self.db_write_batch_enabled:
                self._db_write_queue = asyncio.Queue(maxsize=self.db_write_queue_max)
                self._db_write_worker_task = asyncio.create_task(
                    self._db_write_loop(),
                    name=f"agentnet-registry-db-writer-{self.server_id}",
                )

        self._nc = NATS()
        await self._nc.connect(servers=[self.nats_url], name="agentnet-registry")

        await self._nc.subscribe(REGISTRY_REGISTER_SUBJECT, cb=self._instrument_handler("register", self._on_register))
        await self._nc.subscribe(REGISTRY_HELLO_SUBJECT, cb=self._instrument_handler("hello", self._on_hello))
        await self._nc.subscribe(REGISTRY_GOODBYE_SUBJECT, cb=self._instrument_handler("goodbye", self._on_goodbye))
        await self._nc.subscribe(REGISTRY_LIST_SUBJECT, cb=self._instrument_handler("list", self._on_list))
        await self._nc.subscribe(
            REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
            cb=self._instrument_handler("resolve_account", self._on_resolve_account),
        )
        await self._nc.subscribe(
            REGISTRY_RESOLVE_KEY_SUBJECT,
            cb=self._instrument_handler("resolve_key", self._on_resolve_key),
        )
        await self._nc.subscribe(REGISTRY_SEARCH_SUBJECT, cb=self._instrument_handler("search", self._on_search))
        await self._nc.subscribe(REGISTRY_PROFILE_SUBJECT, cb=self._instrument_handler("profile", self._on_profile))
        await self._nc.subscribe(
            REGISTRY_THREAD_STATUS_SUBJECT,
            cb=self._instrument_handler("thread_status", self._on_thread_status),
        )
        await self._nc.subscribe(
            REGISTRY_THREAD_LIST_SUBJECT,
            cb=self._instrument_handler("thread_list", self._on_thread_list),
        )
        await self._nc.subscribe(
            REGISTRY_THREAD_MESSAGES_SUBJECT,
            cb=self._instrument_handler("thread_messages", self._on_thread_messages),
        )
        await self._nc.subscribe(
            REGISTRY_MESSAGE_SEARCH_SUBJECT,
            cb=self._instrument_handler("message_search", self._on_message_search),
        )
        await self._nc.subscribe(
            REGISTRY_METRICS_SUBJECT,
            cb=self._instrument_handler("metrics", self._on_metrics),
        )
        await self._nc.subscribe(
            "account.*.inbox",
            cb=self._instrument_handler("account_message", self._on_account_message),
        )
        await self._nc.subscribe(
            "agent.capability.*",
            cb=self._instrument_handler("account_message", self._on_account_message),
        )
        await self._nc.subscribe(
            "_INBOX.>",
            cb=self._instrument_handler("account_message", self._on_account_message),
        )

        self._gc_task = asyncio.create_task(self._gc_loop(), name="agentnet-registry-gc")
        print(
            f"registry ready: nats={self.nats_url} server_id={self.server_id} "
            f"ttl={self.ttl_seconds}s heartbeat={self.heartbeat_interval_seconds}s "
            f"thread_soft={self.thread_soft_limit_tokens} thread_hard={self.thread_hard_limit_tokens} "
            f"thread_keep_tail={self.thread_keep_tail_messages} "
            f"db_write_batch={'enabled' if self._db_write_queue is not None else 'disabled'} "
            f"db={'enabled' if self._store else 'disabled'} "
            f"dev_auth={'enabled' if self.dev_auth_enabled else 'disabled'}"
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

        await self._stop_db_writer()

        if self._store is not None:
            await self._store.stop()

    def _instrument_handler(
        self,
        name: str,
        handler: Callable[[Msg], Awaitable[None]],
    ) -> Callable[[Msg], Awaitable[None]]:
        async def wrapped(msg: Msg) -> None:
            started = time.perf_counter()
            self._handler_calls[name] += 1
            try:
                await handler(msg)
            except Exception:
                self._handler_errors[name] += 1
                raise
            finally:
                self._record_latency(f"handler.{name}", (time.perf_counter() - started) * 1000.0)

        return wrapped

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
        self._latency_samples[metric_name].append(max(0.0, float(elapsed_ms)))

    def _latency_stats(self, metric_name: str) -> dict[str, float | int]:
        values = list(self._latency_samples.get(metric_name, ()))
        if not values:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
        values.sort()
        return {
            "count": len(values),
            "p50": round(self._percentile(values, 50), 2),
            "p95": round(self._percentile(values, 95), 2),
            "p99": round(self._percentile(values, 99), 2),
        }

    def _metrics_snapshot(self) -> dict[str, Any]:
        latency_ms: dict[str, dict[str, float | int]] = {}
        for metric_name in sorted(self._latency_samples.keys()):
            latency_ms[metric_name] = self._latency_stats(metric_name)

        handlers: dict[str, dict[str, int]] = {}
        for name in sorted(set(self._handler_calls.keys()) | set(self._handler_errors.keys())):
            handlers[name] = {
                "calls": int(self._handler_calls.get(name, 0)),
                "errors": int(self._handler_errors.get(name, 0)),
            }

        return {
            "generated_at": utc_now_iso(),
            "server_id": self.server_id,
            "window_size": self._metrics_window_size,
            "store_enabled": self._store is not None,
            "db_write_queue": {
                "enabled": self._db_write_queue is not None,
                "size": self._db_write_queue.qsize() if self._db_write_queue is not None else 0,
                "max": self.db_write_queue_max,
                "batch_size": self.db_write_batch_size,
                "flush_ms": round(self.db_write_flush_seconds * 1000.0, 2),
                "overflow_count": self._db_write_queue_overflow_count,
            },
            "latency_ms": latency_ms,
            "handlers": handlers,
        }

    async def _stop_db_writer(self) -> None:
        queue = self._db_write_queue
        task = self._db_write_worker_task
        if queue is None or task is None:
            self._db_write_queue = None
            self._db_write_worker_task = None
            return
        try:
            await queue.put(None)
            await task
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed stopping DB write worker")
        finally:
            self._db_write_queue = None
            self._db_write_worker_task = None

    async def _enqueue_db_write(self, message: AgentMessage, received_at: datetime) -> None:
        if self._store is None:
            return
        queue = self._db_write_queue
        if queue is None:
            started = time.perf_counter()
            await self._store.persist_message(message, received_at=received_at)
            self._record_latency("db.persist_message_inline", (time.perf_counter() - started) * 1000.0)
            return
        try:
            queue.put_nowait((message, received_at))
        except asyncio.QueueFull:
            self._db_write_queue_overflow_count += 1
            started = time.perf_counter()
            await self._store.persist_message(message, received_at=received_at)
            self._record_latency("db.persist_message_inline", (time.perf_counter() - started) * 1000.0)

    async def _persist_db_batch(self, batch: list[tuple[AgentMessage, datetime]]) -> None:
        if self._store is None or not batch:
            return
        started = time.perf_counter()
        try:
            await self._store.persist_messages_batch(batch)
            self._record_latency("db.persist_batch", (time.perf_counter() - started) * 1000.0)
            return
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed persisting DB batch size=%s; retrying per-message", len(batch))
            self._record_latency("db.persist_batch_failed", (time.perf_counter() - started) * 1000.0)

        for message, received_at in batch:
            single_started = time.perf_counter()
            try:
                await self._store.persist_message(message, received_at=received_at)
                self._record_latency("db.persist_message_fallback", (time.perf_counter() - single_started) * 1000.0)
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed fallback message persist message_id=%s", message.message_id)
                self._record_latency("db.persist_message_fallback_failed", (time.perf_counter() - single_started) * 1000.0)

    async def _db_write_loop(self) -> None:
        queue = self._db_write_queue
        if queue is None:
            return
        stop_after_flush = False
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            batch: list[tuple[AgentMessage, datetime]] = [item]
            deadline = time.monotonic() + self.db_write_flush_seconds
            while len(batch) < self.db_write_batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if next_item is None:
                    queue.task_done()
                    stop_after_flush = True
                    break
                batch.append(next_item)
            await self._persist_db_batch(batch)
            latest_by_thread: dict[str, AgentMessage] = {}
            for message, _ in batch:
                thread_id = str(message.thread_id or "").strip()
                if thread_id:
                    latest_by_thread[thread_id] = message
            compaction_started = time.perf_counter()
            for message in latest_by_thread.values():
                await self._maybe_emit_compaction_event(trigger_message=message)
            self._record_latency("compaction.maybe_emit", (time.perf_counter() - compaction_started) * 1000.0)
            for _ in batch:
                queue.task_done()
            if stop_after_flush:
                break

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
        try:
            await self._validate_register_auth(
                data=data,
                agent=agent,
                account_id=account_id,
                username=username,
                now=now_dt,
            )
        except ValueError as exc:
            await self._nc.publish(msg.reply, encode_json({"error": str(exc)}))
            return
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed validating register auth")
            await self._nc.publish(msg.reply, encode_json({"error": "auth_validation_failed"}))
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

    async def _on_resolve_key(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "resolve payload must be an object"}))
            return
        account_id = str(data.get("account_id") or "").strip()
        if not account_id:
            await self._nc.publish(msg.reply, encode_json({"error": "account_id is required"}))
            return
        try:
            public_key = await self._get_bound_dev_public_key(account_id)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed resolving dev auth public key")
            await self._nc.publish(msg.reply, encode_json({"error": "resolve_failed"}))
            return
        if not public_key:
            await self._nc.publish(msg.reply, encode_json({"error": "not_found"}))
            return
        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "account_id": account_id,
                    "public_key": public_key,
                    "resolved_at": utc_now_iso(),
                }
            ),
        )

    async def _on_search(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        await self._evict_stale()
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "search payload must be an object"}))
            return
        query = str(data.get("query") or "")
        capability = str(data.get("capability") or "").strip() or None
        try:
            limit = int(data.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        online_only = bool(data.get("online_only", False))
        try:
            results = await self._search_accounts(
                query=query,
                capability=capability,
                limit=limit,
                online_only=online_only,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed search")
            await self._nc.publish(msg.reply, encode_json({"error": "search_failed"}))
            return
        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "query": query,
                    "capability": capability,
                    "limit": max(1, min(limit, 100)),
                    "online_only": online_only,
                    "results": results,
                    "generated_at": utc_now_iso(),
                }
            ),
        )

    async def _on_profile(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        await self._evict_stale()
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "profile payload must be an object"}))
            return
        account_id = str(data.get("account_id") or "").strip() or None
        username_raw = str(data.get("username") or "")
        username = normalize_username(username_raw) if username_raw else None
        if not account_id and not username:
            await self._nc.publish(msg.reply, encode_json({"error": "account_id or username is required"}))
            return
        try:
            profile = await self._get_account_profile(account_id=account_id, username=username)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed profile lookup")
            await self._nc.publish(msg.reply, encode_json({"error": "profile_failed"}))
            return
        if profile is None:
            await self._nc.publish(msg.reply, encode_json({"error": "not_found"}))
            return
        await self._nc.publish(msg.reply, encode_json({"profile": profile, "generated_at": utc_now_iso()}))

    async def _on_thread_status(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "thread_status payload must be an object"}))
            return
        thread_id = str(data.get("thread_id") or "").strip()
        if not thread_id:
            await self._nc.publish(msg.reply, encode_json({"error": "thread_id is required"}))
            return

        soft_limit_tokens = _coerce_non_negative_int(
            data.get("soft_limit_tokens"),
            default=self.thread_soft_limit_tokens,
        )
        hard_limit_tokens = _coerce_non_negative_int(
            data.get("hard_limit_tokens"),
            default=self.thread_hard_limit_tokens,
        )
        soft_limit_tokens = max(1, soft_limit_tokens)
        hard_limit_tokens = max(soft_limit_tokens, hard_limit_tokens)

        try:
            thread = await self._get_thread_status(thread_id)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed thread status lookup thread_id=%s", thread_id)
            await self._nc.publish(msg.reply, encode_json({"error": "thread_status_failed"}))
            return
        if thread is None:
            await self._nc.publish(msg.reply, encode_json({"error": "not_found"}))
            return

        approx_tokens = _coerce_non_negative_int(thread.get("approx_tokens"))
        message_count = _coerce_non_negative_int(thread.get("message_count"))
        latest_checkpoint_end = _coerce_non_negative_int(thread.get("latest_checkpoint_end"))
        pending_messages = max(0, message_count - latest_checkpoint_end)
        if pending_messages <= self.thread_keep_tail_messages:
            status = "ok"
        else:
            status = _classify_thread_status(approx_tokens, soft_limit_tokens, hard_limit_tokens)
        response = {
            **thread,
            "status": status,
            "pending_messages": pending_messages,
            "soft_limit_tokens": soft_limit_tokens,
            "hard_limit_tokens": hard_limit_tokens,
            "thread_keep_tail_messages": self.thread_keep_tail_messages,
            "token_estimate_chars_per_token": self.token_estimate_chars_per_token,
            "generated_at": utc_now_iso(),
        }
        await self._nc.publish(msg.reply, encode_json(response))

    async def _on_thread_list(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "thread_list payload must be an object"}))
            return

        query = str(data.get("query") or "")
        try:
            limit = int(data.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        safe_limit = max(1, min(limit, 100))

        participant_account_id = str(data.get("participant_account_id") or "").strip() or None
        participant_username_raw = str(data.get("participant_username") or "")
        participant_username = normalize_username(participant_username_raw) if participant_username_raw else None
        if participant_username and not participant_account_id:
            try:
                resolved = await self._resolve_account_by_username(participant_username)
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed resolving participant_username during thread_list")
                await self._nc.publish(msg.reply, encode_json({"error": "thread_list_failed"}))
                return
            if resolved is None:
                response = {
                    "query": query,
                    "limit": safe_limit,
                    "participant_account_id": None,
                    "participant_username": participant_username,
                    "results": [],
                    "generated_at": utc_now_iso(),
                }
                await self._nc.publish(msg.reply, encode_json(response))
                return
            participant_account_id = resolved[0]

        soft_limit_tokens = _coerce_non_negative_int(
            data.get("soft_limit_tokens"),
            default=self.thread_soft_limit_tokens,
        )
        hard_limit_tokens = _coerce_non_negative_int(
            data.get("hard_limit_tokens"),
            default=self.thread_hard_limit_tokens,
        )
        soft_limit_tokens = max(1, soft_limit_tokens)
        hard_limit_tokens = max(soft_limit_tokens, hard_limit_tokens)

        try:
            threads = await self._list_threads(
                participant_account_id=participant_account_id,
                query=query,
                limit=safe_limit,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed thread list lookup")
            await self._nc.publish(msg.reply, encode_json({"error": "thread_list_failed"}))
            return

        results: list[dict[str, Any]] = []
        for item in threads:
            approx_tokens = _coerce_non_negative_int(item.get("approx_tokens"))
            message_count = _coerce_non_negative_int(item.get("message_count"))
            latest_checkpoint_end = _coerce_non_negative_int(item.get("latest_checkpoint_end"))
            pending_messages = max(0, message_count - latest_checkpoint_end)
            if pending_messages <= self.thread_keep_tail_messages:
                status = "ok"
            else:
                status = _classify_thread_status(approx_tokens, soft_limit_tokens, hard_limit_tokens)
            results.append(
                {
                    **item,
                    "status": status,
                    "pending_messages": pending_messages,
                }
            )

        response = {
            "query": query,
            "limit": safe_limit,
            "participant_account_id": participant_account_id,
            "participant_username": participant_username,
            "soft_limit_tokens": soft_limit_tokens,
            "hard_limit_tokens": hard_limit_tokens,
            "token_estimate_chars_per_token": self.token_estimate_chars_per_token,
            "results": results,
            "generated_at": utc_now_iso(),
        }
        await self._nc.publish(msg.reply, encode_json(response))

    async def _on_thread_messages(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "thread_messages payload must be an object"}))
            return
        thread_id = str(data.get("thread_id") or "").strip()
        if not thread_id:
            await self._nc.publish(msg.reply, encode_json({"error": "thread_id is required"}))
            return
        try:
            limit = int(data.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        safe_limit = max(1, min(limit, 200))
        cursor_sent_at, cursor_message_id = _decode_cursor(str(data.get("cursor") or ""))
        try:
            messages, next_cursor = await self._list_thread_messages(
                thread_id=thread_id,
                limit=safe_limit,
                cursor_sent_at=cursor_sent_at,
                cursor_message_id=cursor_message_id,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed thread message lookup thread_id=%s", thread_id)
            await self._nc.publish(msg.reply, encode_json({"error": "thread_messages_failed"}))
            return
        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "thread_id": thread_id,
                    "limit": safe_limit,
                    "cursor": str(data.get("cursor") or "") or None,
                    "next_cursor": next_cursor,
                    "messages": messages,
                    "generated_at": utc_now_iso(),
                }
            ),
        )

    async def _on_message_search(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            await self._nc.publish(msg.reply, encode_json({"error": "message_search payload must be an object"}))
            return

        thread_id = str(data.get("thread_id") or "").strip() or None
        from_account_id = str(data.get("from_account_id") or "").strip() or None
        to_account_id = str(data.get("to_account_id") or "").strip() or None
        kind = str(data.get("kind") or "").strip() or None
        from_ts = parse_iso_utc(str(data.get("from_ts") or "")) if data.get("from_ts") else None
        to_ts = parse_iso_utc(str(data.get("to_ts") or "")) if data.get("to_ts") else None
        if data.get("from_ts") and from_ts is None:
            await self._nc.publish(msg.reply, encode_json({"error": "from_ts_invalid"}))
            return
        if data.get("to_ts") and to_ts is None:
            await self._nc.publish(msg.reply, encode_json({"error": "to_ts_invalid"}))
            return
        try:
            limit = int(data.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        safe_limit = max(1, min(limit, 200))
        cursor_sent_at, cursor_message_id = _decode_cursor(str(data.get("cursor") or ""))
        try:
            messages, next_cursor = await self._search_messages(
                thread_id=thread_id,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                kind=kind,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=safe_limit,
                cursor_sent_at=cursor_sent_at,
                cursor_message_id=cursor_message_id,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed message search")
            await self._nc.publish(msg.reply, encode_json({"error": "message_search_failed"}))
            return
        await self._nc.publish(
            msg.reply,
            encode_json(
                {
                    "filters": {
                        "thread_id": thread_id,
                        "from_account_id": from_account_id,
                        "to_account_id": to_account_id,
                        "kind": kind,
                        "from_ts": str(data.get("from_ts") or "") or None,
                        "to_ts": str(data.get("to_ts") or "") or None,
                    },
                    "limit": safe_limit,
                    "cursor": str(data.get("cursor") or "") or None,
                    "next_cursor": next_cursor,
                    "messages": messages,
                    "generated_at": utc_now_iso(),
                }
            ),
        )

    async def _on_metrics(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return
        await self._nc.publish(msg.reply, encode_json(self._metrics_snapshot()))

    async def _on_account_message(self, msg: Msg) -> None:
        try:
            data = decode_json(msg.data)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(data, dict):
            return
        try:
            message = AgentMessage.from_dict(data)
        except Exception:  # noqa: BLE001
            return
        if not message.message_id:
            return
        if not message.thread_id:
            message.thread_id = f"thread_{(message.trace_id or message.message_id).lower()}"
        if self._store is not None:
            enqueue_started = time.perf_counter()
            try:
                await self._enqueue_db_write(message, received_at=_utc_now())
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed enqueueing message write message_id=%s", message.message_id)
            finally:
                self._record_latency("db.enqueue_message", (time.perf_counter() - enqueue_started) * 1000.0)
            return
        local_persist_started = time.perf_counter()
        self._persist_message_local(message)
        self._record_latency("local.persist_message", (time.perf_counter() - local_persist_started) * 1000.0)
        compaction_started = time.perf_counter()
        await self._maybe_emit_compaction_event(trigger_message=message)
        self._record_latency("compaction.maybe_emit", (time.perf_counter() - compaction_started) * 1000.0)

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(self.gc_interval_seconds)
            self._cleanup_dev_auth_state()
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
        now_iso = utc_now_iso()
        desired_username = normalize_username(requested_username or "")
        if not desired_username:
            desired_username = normalize_username(agent.agent_id) or f"agent_{new_ulid()[:8].lower()}"

        if requested_account_id:
            existing = self._local_accounts_by_id.get(requested_account_id)
            if existing:
                username = str(existing.get("username") or desired_username)
                existing.update(
                    {
                        "username": username,
                        "agent_id": agent.agent_id,
                        "display_name": agent.name,
                        "capabilities": list(agent.capabilities),
                        "metadata": dict(agent.metadata),
                        "updated_at": now_iso,
                    }
                )
                self._local_account_id_by_username[username] = requested_account_id
                return requested_account_id, username

            username = desired_username
            if username in self._local_account_id_by_username:
                username = f"{username[:24]}-{new_ulid()[:6].lower()}".strip("-")
            self._local_accounts_by_id[requested_account_id] = {
                "username": username,
                "agent_id": agent.agent_id,
                "display_name": agent.name,
                "bio": "",
                "capabilities": list(agent.capabilities),
                "metadata": dict(agent.metadata),
                "visibility": "public",
                "status": "active",
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            self._local_account_id_by_username[username] = requested_account_id
            return requested_account_id, username

        existing_account_id = self._local_account_id_by_username.get(desired_username)
        if existing_account_id:
            existing = self._local_accounts_by_id.get(existing_account_id)
            if existing is not None:
                existing.update(
                    {
                        "agent_id": agent.agent_id,
                        "display_name": agent.name,
                        "capabilities": list(agent.capabilities),
                        "metadata": dict(agent.metadata),
                        "updated_at": utc_now_iso(),
                    }
                )
            return existing_account_id, desired_username

        account_id = f"acct_{new_ulid().lower()}"
        self._local_accounts_by_id[account_id] = {
            "username": desired_username,
            "agent_id": agent.agent_id,
            "display_name": agent.name,
            "bio": "",
            "capabilities": list(agent.capabilities),
            "metadata": dict(agent.metadata),
            "visibility": "public",
            "status": "active",
            "created_at": now_iso,
            "updated_at": now_iso,
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

    async def _search_accounts(
        self,
        *,
        query: str,
        capability: str | None,
        limit: int,
        online_only: bool,
    ) -> list[dict[str, Any]]:
        if self._store is not None:
            return await self._store.search_accounts(
                query=query,
                capability=capability,
                limit=limit,
                online_only=online_only,
            )
        needle = query.strip().lower()
        capability_needle = (capability or "").strip().lower()
        safe_limit = max(1, min(limit, 100))
        results: list[dict[str, Any]] = []
        for account_id, account in self._local_accounts_by_id.items():
            username = str(account.get("username") or "")
            display_name = str(account.get("display_name") or username)
            caps = account.get("capabilities") if isinstance(account.get("capabilities"), list) else []
            if needle and needle not in username.lower() and needle not in display_name.lower():
                continue
            if capability_needle and capability_needle not in {str(c).lower() for c in caps}:
                continue
            online_sessions = [
                session
                for session in self._sessions.values()
                if session.account_id == account_id
            ]
            if online_only and not online_sessions:
                continue
            results.append(
                {
                    "account_id": account_id,
                    "username": username,
                    "display_name": display_name,
                    "capabilities": caps,
                    "metadata": account.get("metadata") if isinstance(account.get("metadata"), dict) else {},
                    "visibility": str(account.get("visibility") or "public"),
                    "status": str(account.get("status") or "active"),
                    "online_sessions": len(online_sessions),
                    "online": bool(online_sessions),
                }
            )
        results.sort(key=lambda item: (-int(item.get("online_sessions") or 0), str(item.get("username") or "")))
        return results[:safe_limit]

    async def _get_account_profile(
        self,
        *,
        account_id: str | None,
        username: str | None,
    ) -> dict[str, Any] | None:
        if self._store is not None:
            return await self._store.get_account_profile(account_id=account_id, username=username)
        resolved_account_id = account_id
        if not resolved_account_id and username:
            resolved_account_id = self._local_account_id_by_username.get(normalize_username(username))
        if not resolved_account_id:
            return None
        account = self._local_accounts_by_id.get(resolved_account_id)
        if account is None:
            return None
        sessions = [info for info in self._sessions.values() if info.account_id == resolved_account_id]
        sessions.sort(key=lambda item: item.last_seen or "", reverse=True)
        return {
            "account_id": resolved_account_id,
            "username": str(account.get("username") or ""),
            "display_name": str(account.get("display_name") or account.get("username") or ""),
            "bio": str(account.get("bio") or ""),
            "capabilities": account.get("capabilities") if isinstance(account.get("capabilities"), list) else [],
            "metadata": account.get("metadata") if isinstance(account.get("metadata"), dict) else {},
            "visibility": str(account.get("visibility") or "public"),
            "status": str(account.get("status") or "active"),
            "created_at": str(account.get("created_at") or ""),
            "updated_at": str(account.get("updated_at") or ""),
            "online_sessions": [
                {
                    "session_tag": session.session_tag,
                    "agent_id": session.agent_id,
                    "server_id": self.server_id,
                    "connected_at": "",
                    "last_seen": session.last_seen,
                    "capabilities": session.capabilities,
                    "metadata": session.metadata,
                }
                for session in sessions
            ],
            "online": bool(sessions),
        }

    async def _get_thread_status(self, thread_id: str) -> dict[str, Any] | None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return None
        if self._store is not None:
            return await self._store.get_thread_status(normalized_thread_id)
        thread = self._local_threads.get(normalized_thread_id)
        if thread is None:
            return None
        return {
            "thread_id": normalized_thread_id,
            "created_by_account_id": str(thread.get("created_by_account_id") or "") or None,
            "participants": thread.get("participants") if isinstance(thread.get("participants"), list) else [],
            "created_at": str(thread.get("created_at") or ""),
            "updated_at": str(thread.get("updated_at") or ""),
            "message_count": _coerce_non_negative_int(thread.get("message_count")),
            "byte_count": _coerce_non_negative_int(thread.get("byte_count")),
            "approx_tokens": _coerce_non_negative_int(thread.get("approx_tokens")),
            "latest_checkpoint_end": _coerce_non_negative_int(thread.get("latest_checkpoint_end")),
            "last_message_at": str(thread.get("last_message_at") or "") or None,
        }

    async def _list_threads(
        self,
        *,
        participant_account_id: str | None,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._store is not None:
            return await self._store.list_threads(
                participant_account_id=participant_account_id,
                query=query,
                limit=limit,
            )
        normalized_query = str(query or "").strip().lower()
        participant = str(participant_account_id or "").strip()
        safe_limit = max(1, min(limit, 100))
        rows: list[dict[str, Any]] = []
        for thread_id, thread in self._local_threads.items():
            if normalized_query and normalized_query not in thread_id.lower():
                continue
            participants = thread.get("participants") if isinstance(thread.get("participants"), list) else []
            participant_values = [str(item) for item in participants if str(item).strip()]
            if participant and participant not in participant_values:
                continue
            rows.append(
                {
                    "thread_id": thread_id,
                    "created_by_account_id": str(thread.get("created_by_account_id") or "") or None,
                    "participants": participant_values,
                    "created_at": str(thread.get("created_at") or ""),
                    "updated_at": str(thread.get("updated_at") or ""),
                    "message_count": _coerce_non_negative_int(thread.get("message_count")),
                    "byte_count": _coerce_non_negative_int(thread.get("byte_count")),
                    "approx_tokens": _coerce_non_negative_int(thread.get("approx_tokens")),
                    "latest_checkpoint_end": _coerce_non_negative_int(thread.get("latest_checkpoint_end")),
                    "last_message_at": str(thread.get("last_message_at") or "") or None,
                }
            )
        rows.sort(
            key=lambda item: (
                str(item.get("last_message_at") or item.get("updated_at") or ""),
                str(item.get("thread_id") or ""),
            ),
            reverse=True,
        )
        return rows[:safe_limit]

    async def _list_thread_messages(
        self,
        *,
        thread_id: str,
        limit: int,
        cursor_sent_at: datetime | None,
        cursor_message_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._store is not None:
            return await self._store.list_thread_messages(
                thread_id=thread_id,
                limit=limit,
                cursor_sent_at=cursor_sent_at,
                cursor_message_id=cursor_message_id,
            )
        normalized_thread_id = str(thread_id or "").strip()
        safe_limit = max(1, min(limit, 200))
        rows: list[dict[str, Any]] = []
        for message in self._local_messages.values():
            if str(message.get("thread_id") or "") != normalized_thread_id:
                continue
            sent_at_raw = str(message.get("sent_at") or "")
            sent_at_dt = parse_iso_utc(sent_at_raw)
            if cursor_sent_at is not None:
                if sent_at_dt is None:
                    continue
                message_id = str(message.get("message_id") or "")
                if not (sent_at_dt < cursor_sent_at or (sent_at_dt == cursor_sent_at and message_id < (cursor_message_id or ""))):
                    continue
            rows.append(message)
        rows.sort(
            key=lambda item: (
                str(item.get("sent_at") or ""),
                str(item.get("message_id") or ""),
            ),
            reverse=True,
        )
        selected = rows[: safe_limit + 1]
        has_more = len(selected) > safe_limit
        slice_rows = selected[:safe_limit]
        messages = [
            {
                "message_id": str(item.get("message_id") or ""),
                "thread_id": str(item.get("thread_id") or ""),
                "parent_message_id": str(item.get("parent_message_id")) if item.get("parent_message_id") else None,
                "from_account_id": str(item.get("from_account_id")) if item.get("from_account_id") else None,
                "from_session_tag": str(item.get("from_session_tag")) if item.get("from_session_tag") else None,
                "to_account_id": str(item.get("to_account_id")) if item.get("to_account_id") else None,
                "to_agent": str(item.get("to_agent") or ""),
                "kind": str(item.get("kind") or ""),
                "schema_version": str(item.get("schema_version") or "1.0"),
                "idempotency_key": str(item.get("idempotency_key")) if item.get("idempotency_key") else None,
                "payload": item.get("payload"),
                "trace_id": str(item.get("trace_id")) if item.get("trace_id") else None,
                "sent_at": str(item.get("sent_at") or ""),
                "received_at": str(item.get("received_at") or ""),
                "status": "received",
                "metadata": {},
            }
            for item in slice_rows
        ]
        next_cursor: str | None = None
        if has_more and slice_rows:
            last_item = slice_rows[-1]
            next_cursor = _encode_cursor(
                sent_at=str(last_item.get("sent_at") or ""),
                message_id=str(last_item.get("message_id") or ""),
            )
        return messages, next_cursor

    async def _search_messages(
        self,
        *,
        thread_id: str | None,
        from_account_id: str | None,
        to_account_id: str | None,
        kind: str | None,
        from_ts: datetime | None,
        to_ts: datetime | None,
        limit: int,
        cursor_sent_at: datetime | None,
        cursor_message_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._store is not None:
            return await self._store.search_messages(
                thread_id=thread_id,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                kind=kind,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=limit,
                cursor_sent_at=cursor_sent_at,
                cursor_message_id=cursor_message_id,
            )
        normalized_thread_id = str(thread_id or "").strip()
        normalized_from = str(from_account_id or "").strip()
        normalized_to = str(to_account_id or "").strip()
        normalized_kind = str(kind or "").strip()
        safe_limit = max(1, min(limit, 200))
        rows: list[dict[str, Any]] = []
        for message in self._local_messages.values():
            if normalized_thread_id and str(message.get("thread_id") or "") != normalized_thread_id:
                continue
            if normalized_from and str(message.get("from_account_id") or "") != normalized_from:
                continue
            if normalized_to and str(message.get("to_account_id") or "") != normalized_to:
                continue
            if normalized_kind and str(message.get("kind") or "") != normalized_kind:
                continue
            sent_at_raw = str(message.get("sent_at") or "")
            sent_at_dt = parse_iso_utc(sent_at_raw)
            if from_ts is not None:
                if sent_at_dt is None or sent_at_dt < from_ts:
                    continue
            if to_ts is not None:
                if sent_at_dt is None or sent_at_dt > to_ts:
                    continue
            if cursor_sent_at is not None:
                if sent_at_dt is None:
                    continue
                message_id = str(message.get("message_id") or "")
                if not (sent_at_dt < cursor_sent_at or (sent_at_dt == cursor_sent_at and message_id < (cursor_message_id or ""))):
                    continue
            rows.append(message)
        rows.sort(
            key=lambda item: (
                str(item.get("sent_at") or ""),
                str(item.get("message_id") or ""),
            ),
            reverse=True,
        )
        selected = rows[: safe_limit + 1]
        has_more = len(selected) > safe_limit
        slice_rows = selected[:safe_limit]
        messages = [
            {
                "message_id": str(item.get("message_id") or ""),
                "thread_id": str(item.get("thread_id") or ""),
                "parent_message_id": str(item.get("parent_message_id")) if item.get("parent_message_id") else None,
                "from_account_id": str(item.get("from_account_id")) if item.get("from_account_id") else None,
                "from_session_tag": str(item.get("from_session_tag")) if item.get("from_session_tag") else None,
                "to_account_id": str(item.get("to_account_id")) if item.get("to_account_id") else None,
                "to_agent": str(item.get("to_agent") or ""),
                "kind": str(item.get("kind") or ""),
                "schema_version": str(item.get("schema_version") or "1.0"),
                "idempotency_key": str(item.get("idempotency_key")) if item.get("idempotency_key") else None,
                "payload": item.get("payload"),
                "trace_id": str(item.get("trace_id")) if item.get("trace_id") else None,
                "sent_at": str(item.get("sent_at") or ""),
                "received_at": str(item.get("received_at") or ""),
                "status": "received",
                "metadata": {},
            }
            for item in slice_rows
        ]
        next_cursor: str | None = None
        if has_more and slice_rows:
            last_item = slice_rows[-1]
            next_cursor = _encode_cursor(
                sent_at=str(last_item.get("sent_at") or ""),
                message_id=str(last_item.get("message_id") or ""),
            )
        return messages, next_cursor

    def _persist_message_local(self, message: AgentMessage) -> None:
        thread_id = str(message.thread_id or "").strip() or f"thread_{(message.trace_id or message.message_id).lower()}"
        is_new_message = message.message_id not in self._local_messages
        existing_thread = self._local_threads.get(thread_id)
        participants = set()
        if existing_thread is not None:
            existing_participants = existing_thread.get("participants")
            if isinstance(existing_participants, list):
                participants.update(str(item) for item in existing_participants if str(item).strip())
        for participant in (message.from_account_id, message.to_account_id):
            participant_value = str(participant or "").strip()
            if participant_value:
                participants.add(participant_value)

        payload_value = message.payload
        try:
            payload_json = json.dumps(payload_value)
        except TypeError:
            payload_json = json.dumps({"text": str(payload_value)})
        payload_bytes = len(payload_json.encode("utf-8"))
        payload_tokens = _estimate_tokens(payload_bytes, self.token_estimate_chars_per_token)
        checkpoint_end = _extract_checkpoint_end(payload_value)
        sent_at = str(message.sent_at or "").strip() or utc_now_iso()
        now_iso = utc_now_iso()
        if existing_thread is None:
            existing_thread = {
                "thread_id": thread_id,
                "created_by_account_id": message.from_account_id,
                "participants": sorted(participants),
                "message_count": 0,
                "byte_count": 0,
                "approx_tokens": 0,
                "latest_checkpoint_end": 0,
                "last_message_at": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            self._local_threads[thread_id] = existing_thread
        else:
            existing_thread["participants"] = sorted(participants)
            if not str(existing_thread.get("created_by_account_id") or "").strip():
                existing_thread["created_by_account_id"] = message.from_account_id
            existing_thread["updated_at"] = now_iso

        if is_new_message:
            existing_thread["message_count"] = _coerce_non_negative_int(existing_thread.get("message_count")) + 1
            existing_thread["byte_count"] = _coerce_non_negative_int(existing_thread.get("byte_count")) + payload_bytes
            existing_thread["approx_tokens"] = _coerce_non_negative_int(existing_thread.get("approx_tokens")) + payload_tokens
            existing_thread["latest_checkpoint_end"] = max(
                _coerce_non_negative_int(existing_thread.get("latest_checkpoint_end")),
                checkpoint_end,
            )
            existing_thread["last_message_at"] = sent_at
            existing_thread["updated_at"] = now_iso

        self._local_messages[message.message_id] = {
            "message_id": message.message_id,
            "thread_id": thread_id,
            "parent_message_id": message.parent_message_id,
            "from_account_id": message.from_account_id,
            "from_session_tag": message.from_session_tag,
            "to_account_id": message.to_account_id,
            "to_agent": message.to_agent,
            "payload": message.payload,
            "trace_id": message.trace_id,
            "kind": message.kind,
            "schema_version": message.schema_version,
            "idempotency_key": message.idempotency_key,
            "sent_at": message.sent_at,
            "received_at": now_iso,
            "byte_count": payload_bytes,
            "approx_tokens": payload_tokens,
        }

    async def _validate_register_auth(
        self,
        *,
        data: dict[str, Any],
        agent: AgentInfo,
        account_id: str,
        username: str,
        now: datetime,
    ) -> None:
        if not self.dev_auth_enabled:
            return

        auth = data.get("auth")
        if not isinstance(auth, dict):
            raise ValueError("auth_required")
        scheme = str(auth.get("scheme") or "")
        if scheme != DEV_AUTH_SCHEME:
            raise ValueError("auth_scheme_invalid")

        public_key = str(auth.get("public_key") or "").strip()
        signature = str(auth.get("signature") or "").strip()
        claims = auth.get("claims")
        if not public_key or not signature or not isinstance(claims, dict):
            raise ValueError("auth_payload_invalid")

        claim_agent_id = str(claims.get("agent_id") or "")
        claim_name = str(claims.get("name") or "")
        claim_username = normalize_username(str(claims.get("username") or ""))
        claim_account_id = str(claims.get("account_id") or "").strip()
        claim_timestamp = str(claims.get("timestamp") or "")
        claim_nonce = str(claims.get("nonce") or "").strip()
        if not claim_timestamp or not claim_nonce:
            raise ValueError("auth_claims_invalid")

        if claim_agent_id != agent.agent_id:
            raise ValueError("auth_claims_agent_mismatch")
        if claim_name != agent.name:
            raise ValueError("auth_claims_name_mismatch")
        if claim_username != normalize_username(username):
            raise ValueError("auth_claims_username_mismatch")
        if claim_account_id and claim_account_id != account_id:
            raise ValueError("auth_claims_account_mismatch")

        timestamp_dt = parse_iso_utc(claim_timestamp)
        if timestamp_dt is None:
            raise ValueError("auth_claims_timestamp_invalid")
        skew = abs((now - timestamp_dt).total_seconds())
        if skew > self.dev_auth_max_skew_seconds:
            raise ValueError("auth_claims_timestamp_skew")

        if not verify_claims(public_key_b64=public_key, claims={k: str(v) for k, v in claims.items()}, signature_b64=signature):
            raise ValueError("auth_signature_invalid")

        nonce_key = f"{account_id}:{claim_nonce}"
        now_mono = time.monotonic()
        nonce_expiry = self._seen_register_nonces.get(nonce_key)
        if nonce_expiry and nonce_expiry > now_mono:
            raise ValueError("auth_nonce_replayed")
        self._seen_register_nonces[nonce_key] = now_mono + self.dev_auth_nonce_ttl_seconds

        existing_key = await self._get_bound_dev_public_key(account_id)
        if existing_key is None:
            await self._bind_dev_public_key(account_id, public_key, now=now)
            return
        if existing_key != public_key:
            raise ValueError("auth_public_key_mismatch")

    async def _get_bound_dev_public_key(self, account_id: str) -> str | None:
        if self._store is not None:
            return await self._store.get_account_dev_public_key(account_id)
        return self._local_dev_public_key_by_account.get(account_id)

    async def _bind_dev_public_key(self, account_id: str, public_key: str, now: datetime) -> None:
        if self._store is not None:
            await self._store.bind_account_dev_public_key(account_id, public_key, now)
        self._local_dev_public_key_by_account[account_id] = public_key

    def _cleanup_dev_auth_state(self) -> None:
        if not self._seen_register_nonces:
            return
        now_mono = time.monotonic()
        expired = [nonce for nonce, expiry in self._seen_register_nonces.items() if expiry <= now_mono]
        for nonce in expired:
            self._seen_register_nonces.pop(nonce, None)

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

    @staticmethod
    def _is_compaction_event_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        payload_type = str(payload.get("type") or "").strip().lower()
        return payload_type in {"compaction_required", "checkpoint"}

    def _has_online_agent_session(self, account_id: str) -> bool:
        normalized = str(account_id or "").strip()
        if not normalized:
            return False
        for info in self._sessions.values():
            if info.account_id == normalized and bool(info.capabilities):
                return True
        return False

    def _has_online_session(self, account_id: str) -> bool:
        normalized = str(account_id or "").strip()
        if not normalized:
            return False
        for info in self._sessions.values():
            if info.account_id == normalized:
                return True
        return False

    def _choose_compaction_target_account(
        self,
        *,
        thread: dict[str, Any],
        trigger_message: AgentMessage,
    ) -> str | None:
        participants = thread.get("participants") if isinstance(thread.get("participants"), list) else []
        participant_ids = [str(item).strip() for item in participants if str(item).strip()]
        preferred = [
            str(trigger_message.to_account_id or "").strip(),
            str(trigger_message.from_account_id or "").strip(),
        ]
        for candidate in preferred:
            if candidate and self._has_online_agent_session(candidate):
                return candidate
        for candidate in participant_ids:
            if self._has_online_agent_session(candidate):
                return candidate
        for candidate in preferred:
            if candidate and self._has_online_session(candidate):
                return candidate
        for candidate in participant_ids:
            if self._has_online_session(candidate):
                return candidate
        fallback = str(thread.get("created_by_account_id") or "").strip()
        if fallback:
            return fallback
        for candidate in preferred:
            if candidate:
                return candidate
        return participant_ids[0] if participant_ids else None

    async def _maybe_emit_compaction_event(self, *, trigger_message: AgentMessage) -> None:
        if not self.compaction_event_enabled or not self._nc:
            return
        thread_id = str(trigger_message.thread_id or "").strip()
        if not thread_id:
            return
        if self._is_compaction_event_payload(trigger_message.payload):
            return
        thread = await self._get_thread_status(thread_id)
        if not thread:
            return
        approx_tokens = _coerce_non_negative_int(thread.get("approx_tokens"))
        status = _classify_thread_status(
            approx_tokens=approx_tokens,
            soft_limit_tokens=self.thread_soft_limit_tokens,
            hard_limit_tokens=self.thread_hard_limit_tokens,
        )
        if status != "needs_compaction":
            self._compaction_event_state.pop(thread_id, None)
            return
        latest_checkpoint_end = _coerce_non_negative_int(thread.get("latest_checkpoint_end"))
        message_count = _coerce_non_negative_int(thread.get("message_count"))
        pending_messages = max(0, message_count - latest_checkpoint_end)
        if pending_messages <= self.thread_keep_tail_messages:
            self._compaction_event_state.pop(thread_id, None)
            return
        now_mono = time.monotonic()
        prior = self._compaction_event_state.get(thread_id)
        if prior is not None:
            prior_checkpoint_end, prior_sent_at = prior
            if latest_checkpoint_end <= prior_checkpoint_end and (now_mono - prior_sent_at) < self.compaction_event_cooldown_seconds:
                return
        to_account_id = self._choose_compaction_target_account(thread=thread, trigger_message=trigger_message)
        if not to_account_id:
            return

        sent_at_dt = _utc_now()
        sent_at = _iso_utc(sent_at_dt)
        expires_at = _iso_utc(sent_at_dt + timedelta(seconds=300))
        byte_count = _coerce_non_negative_int(thread.get("byte_count"))
        event_payload = {
            "type": "compaction_required",
            "thread_id": thread_id,
            "status": status,
            "message_count": message_count,
            "pending_messages": pending_messages,
            "byte_count": byte_count,
            "approx_tokens": approx_tokens,
            "soft_limit_tokens": self.thread_soft_limit_tokens,
            "hard_limit_tokens": self.thread_hard_limit_tokens,
            "keep_tail_messages": self.thread_keep_tail_messages,
            "latest_checkpoint_end": latest_checkpoint_end,
            "requested_at": sent_at,
            "reason": "thread_token_limit_exceeded",
        }
        event_message = AgentMessage(
            message_id=f"msg_{new_ulid().lower()}",
            from_agent=f"registry_{self.server_id}",
            to_agent="thread_compactor",
            payload=event_payload,
            sent_at=sent_at,
            from_account_id=f"acct_registry_{self.server_id}",
            to_account_id=to_account_id,
            ttl_ms=300_000,
            expires_at=expires_at,
            trace_id=f"trace_{new_ulid().lower()}",
            thread_id=thread_id,
            parent_message_id=trigger_message.message_id,
            kind="system",
            schema_version="1.1",
            idempotency_key=f"compaction:{thread_id}:{latest_checkpoint_end}:{message_count}",
        )
        try:
            await self._nc.publish(
                account_inbox_subject(to_account_id),
                encode_json(event_message.to_dict()),
            )
            self._compaction_event_state[thread_id] = (latest_checkpoint_end, now_mono)
            self.logger.info(
                "emitted compaction event thread_id=%s to_account_id=%s approx_tokens=%s",
                thread_id,
                to_account_id,
                approx_tokens,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception(
                "failed emitting compaction event thread_id=%s to_account_id=%s",
                thread_id,
                to_account_id,
            )


async def amain() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    nats_url = os.getenv("NATS_URL", DEFAULT_REGISTRY_SERVICE_NATS_URL)
    ttl_seconds = float(os.getenv("AGENT_TTL_SECONDS", "40"))
    gc_interval = float(os.getenv("GC_INTERVAL_SECONDS", "5"))
    heartbeat_interval = float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "12"))
    thread_soft_limit_tokens = int(os.getenv("THREAD_SOFT_LIMIT_TOKENS", "50000"))
    thread_hard_limit_tokens = int(os.getenv("THREAD_HARD_LIMIT_TOKENS", "60000"))
    thread_keep_tail_messages = int(os.getenv("THREAD_KEEP_TAIL_MESSAGES", "24"))
    token_estimate_chars_per_token = float(os.getenv("TOKEN_ESTIMATE_CHARS_PER_TOKEN", "4"))
    server_id = os.getenv("REGISTRY_SERVER_ID", "registry")
    database_url = os.getenv("DATABASE_URL")
    retention_days = float(os.getenv("SESSION_RETENTION_DAYS", "14"))
    dev_auth_enabled = parse_bool(os.getenv("DEV_AUTH"), default=False)
    dev_auth_max_skew_seconds = float(os.getenv("DEV_AUTH_MAX_SKEW_SECONDS", "120"))
    dev_auth_nonce_ttl_seconds = float(os.getenv("DEV_AUTH_NONCE_TTL_SECONDS", "300"))
    compaction_event_enabled = parse_bool(os.getenv("THREAD_COMPACTION_EVENT_ENABLED"), default=True)
    compaction_event_cooldown_seconds = float(os.getenv("THREAD_COMPACTION_EVENT_COOLDOWN_SECONDS", "120"))
    db_write_batch_enabled = parse_bool(os.getenv("DB_WRITE_BATCH_ENABLED"), default=True)
    db_write_batch_size = int(os.getenv("DB_WRITE_BATCH_SIZE", "64"))
    db_write_flush_ms = float(os.getenv("DB_WRITE_FLUSH_MS", "40"))
    db_write_queue_max = int(os.getenv("DB_WRITE_QUEUE_MAX", "10000"))

    service = RegistryService(
        nats_url=nats_url,
        ttl_seconds=ttl_seconds,
        gc_interval_seconds=gc_interval,
        heartbeat_interval_seconds=heartbeat_interval,
        thread_soft_limit_tokens=thread_soft_limit_tokens,
        thread_hard_limit_tokens=thread_hard_limit_tokens,
        thread_keep_tail_messages=thread_keep_tail_messages,
        token_estimate_chars_per_token=token_estimate_chars_per_token,
        server_id=server_id,
        database_url=database_url,
        session_retention_days=retention_days,
        dev_auth_enabled=dev_auth_enabled,
        dev_auth_max_skew_seconds=dev_auth_max_skew_seconds,
        dev_auth_nonce_ttl_seconds=dev_auth_nonce_ttl_seconds,
        compaction_event_enabled=compaction_event_enabled,
        compaction_event_cooldown_seconds=compaction_event_cooldown_seconds,
        db_write_batch_enabled=db_write_batch_enabled,
        db_write_batch_size=db_write_batch_size,
        db_write_flush_ms=db_write_flush_ms,
        db_write_queue_max=db_write_queue_max,
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
