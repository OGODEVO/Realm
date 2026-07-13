#!/usr/bin/env python3
"""Realm MCP Server — exposes AgentNet as MCP tools for AI agents.

Every server instance can act as a coordinator harness. Chat tools still
support current-thread conversation, but delegated work should use the task
tools so background agents can report terminal completion later.

Tools:
  current_thread         — see which thread is active
  new_thread [name]      — start a fresh thread (switches to it)
  switch_thread <id>     — switch to another thread by ID
  list_online            — who's on the network
  get_profile <target>   — look up an agent
  search_profiles <...>  — find agents by keyword/capability
  send_text <to> <text>  — fire-and-forget message (current thread)
  ask_text <to> <text>   — send and wait for reply (current thread)
  delegate_task          — assign background work to another agent
  await_task             — wait for completed/blocked/failed task result
  task_status            — inspect a known task
  list_tasks             — list recent delegated tasks
  get_thread_messages    — read current thread history
  list_threads           — all threads you're part of
  search_messages <...>  — search across all message history
  thread_status [id]     — budget/compaction health
  registry_metrics       — network health stats

Environment:
  REALM_NATS_URL   — NATS connection URL (default: nats://agentnet_secret_token@localhost:4222)
  REALM_AGENT_NAME — agent name on the network (default: medusa-bridge)
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agentnet.exceptions import DeliveryAckTimeout
from agentnet.sdk import AgentSDK
from agentnet.schema import AgentMessage
from agentnet.task_protocol import (
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_RESULT,
    TERMINAL_TASK_TYPES,
    decode_task_payload,
    task_id_from_payload,
    new_task_id,
    task_type,
)
from mcp.server.fastmcp import FastMCP

NATS_URL = os.getenv("REALM_NATS_URL", "nats://agentnet_secret_token@localhost:4222")
AGENT_NAME = os.getenv("REALM_AGENT_NAME", "medusa-bridge")
BLOB_DIR = os.getenv("REALM_BLOB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".blobs"))
DEFAULT_REQUEST_TIMEOUT = float(os.getenv("REALM_DEFAULT_REQUEST_TIMEOUT_SECONDS", "86400"))
WORK_TIMEOUT = float(os.getenv("REALM_WORK_TIMEOUT_SECONDS", "86400"))
# Codex and other MCP clients may enforce their own per-tool-call deadline.
# Keep blocking task waits below that outer deadline and ask the caller to poll.
MAX_BLOCKING_WAIT_SECONDS = float(os.getenv("REALM_MCP_MAX_BLOCKING_WAIT_SECONDS", "240"))

_sdk: AgentSDK | None = None
_current_thread_id: str | None = None
_last_message_id: str | None = None
_task_store: "TaskStore | None" = None
_task_condition: asyncio.Condition | None = None


def _get_sdk() -> AgentSDK:
    if _sdk is None:
        raise RuntimeError("SDK not connected — lifespan not yet started")
    return _sdk


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def _decode_nested_json_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            stripped = text.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    return payload
                if isinstance(decoded, dict):
                    return decoded
    return payload


def _status_for_task_payload(payload: Any) -> str:
    payload_type = task_type(payload)
    if payload_type == TASK_RESULT:
        return str(payload.get("status") or "completed") if isinstance(payload, dict) else "completed"
    if payload_type == TASK_BLOCKED:
        return "blocked"
    if payload_type == TASK_FAILED:
        return "failed"
    if payload_type:
        return payload_type.removeprefix("task.")
    return "unknown"


def _blocking_wait_budget(timeout: float) -> tuple[float, bool]:
    """Return the in-process wait budget and whether it was client-safety capped."""
    requested = max(0.1, float(timeout))
    if MAX_BLOCKING_WAIT_SECONDS <= 0:
        return requested, False
    budget = min(requested, MAX_BLOCKING_WAIT_SECONDS)
    return budget, budget < requested


class TaskStore:
    """Small persistent ledger for MCP-initiated background tasks."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self._lock = asyncio.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        async with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return
            tasks = raw.get("tasks") if isinstance(raw, dict) else None
            if isinstance(tasks, dict):
                self._tasks = {
                    str(task_id): dict(value)
                    for task_id, value in tasks.items()
                    if isinstance(value, dict)
                }

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"tasks": self._tasks}, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    async def upsert(self, task_id: str, **fields: Any) -> dict[str, Any]:
        async with self._lock:
            current = dict(self._tasks.get(task_id, {}))
            current.update({k: v for k, v in fields.items() if v is not None})
            current["task_id"] = task_id
            self._tasks[task_id] = current
        await self.save()
        return current

    async def get(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            current = self._tasks.get(task_id)
            return dict(current) if current is not None else None

    async def list(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [dict(value) for value in self._tasks.values()]
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return rows[: max(1, min(100, int(limit)))]


async def _notify_task_waiters() -> None:
    condition = _task_condition
    if condition is None:
        return
    async with condition:
        condition.notify_all()


async def _record_inbound_task_event(message: AgentMessage) -> None:
    store = _task_store
    if store is None:
        return
    payload = _decode_nested_json_payload(message.payload)
    decoded = decode_task_payload(payload)
    if decoded is None:
        return
    task_id = task_id_from_payload(decoded)
    if not task_id:
        return
    status = _status_for_task_payload(decoded)
    await store.upsert(
        task_id,
        status=status,
        type=task_type(decoded),
        text=str(decoded.get("text") or ""),
        payload=dict(decoded),
        thread_id=message.thread_id,
        from_agent=message.from_agent,
        from_account_id=message.from_account_id,
        message_id=message.message_id,
        updated_at=str(decoded.get("finished_at") or decoded.get("event_at") or ""),
    )
    await _notify_task_waiters()


async def _handle_inbox_message(message: AgentMessage) -> None:
    await _record_inbound_task_event(message)


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _sdk, _current_thread_id, _last_message_id, _task_store, _task_condition
    try:
        _sdk = AgentSDK(
            agent_id=f"mcp_{AGENT_NAME}",
            name=AGENT_NAME,
            username=AGENT_NAME,
            capabilities=["mcp-bridge", "realm-tools"],
            nats_url=NATS_URL,
            metadata={"kind": "mcp-server", "hostname": os.uname().nodename},
            blob_store_dir=BLOB_DIR,
            default_request_timeout=DEFAULT_REQUEST_TIMEOUT,
            work_timeout_seconds=WORK_TIMEOUT,
        )
        _task_store = TaskStore(os.getenv("REALM_TASK_LEDGER", os.path.join(BLOB_DIR, "tasks.json")))
        _task_condition = asyncio.Condition()
        await _task_store.load()
        _sdk.receive(_handle_inbox_message)
        await _sdk.start()
        _current_thread_id = _sdk.new_thread_id()
        _last_message_id = None
        yield
    except Exception:
        raise
    finally:
        if _sdk is not None:
            try:
                await _sdk.stop()
            except Exception:
                pass
            _sdk = None
            _current_thread_id = None
            _last_message_id = None
            _task_store = None
            _task_condition = None


mcp = FastMCP(
    name="realm-mcp",
    instructions=(
        "Realm / AgentNet MCP bridge for multi-agent work. "
        "Jobs: use delegate_task (with parent_task_id when re-delegating). "
        "Visibility: use agent_status(@agent) to see what someone is doing; "
        "workers use report_progress while working; use await_task/task_status for completion. "
        "Use ask_text only for short synchronous conversation, not long jobs."
    ),
    lifespan=lifespan,
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8104")),
)

# ---------------------------------------------------------------------------
# Thread session management
# ---------------------------------------------------------------------------
@mcp.tool()
def current_thread() -> str:
    """Show the active conversation thread."""
    return _json({"thread_id": _current_thread_id, "last_message_id": _last_message_id})


@mcp.tool()
def new_thread(name: str | None = None) -> str:
    """Start a new conversation thread and switch to it.

    name: optional label for the thread (e.g. 'ops-planning', 'debug-session')
    """
    global _current_thread_id, _last_message_id
    sdk = _get_sdk()
    if name:
        _current_thread_id = f"thread_{name.strip().replace(' ', '_')}_{sdk.new_thread_id().split('_')[-1]}"
    else:
        _current_thread_id = sdk.new_thread_id()
    _last_message_id = None
    return _json({"thread_id": _current_thread_id, "action": "switched"})


@mcp.tool()
def switch_thread(thread_id: str) -> str:
    """Switch to an existing thread by ID."""
    global _current_thread_id, _last_message_id
    _current_thread_id = thread_id
    _last_message_id = None
    return _json({"thread_id": _current_thread_id, "action": "switched"})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_online() -> str:
    """List agents currently online (one row per logical identity).

    Metadata includes role, company_visible, and session_count.
    MCP harnesses may show session_count > 1 (many bridge sessions); prefer
    company_visible workers for delegated work, not harness clones.
    """
    agents = await _get_sdk().list_online()
    return _json([a.to_dict() for a in agents])


@mcp.tool()
async def agent_status(target: str, limit: int = 10) -> str:
    """What is this agent doing? Online presence + active/recent tasks + one-line summary.

    Use this for: "What is Daniela doing?" / "Where is Sandra at?"

    target: @username or acct_... account ID
    limit: max recent tasks to include
    """
    return _json(await _get_sdk().agent_status(target, limit=limit))


@mcp.tool()
async def get_profile(target: str) -> str:
    """Get an agent's public profile.

    target: agent ID, @username, or acct_... account ID.
    """
    return _json(await _get_sdk().get_profile(target))


@mcp.tool()
async def search_profiles(
    query: str = "",
    capability: str | None = None,
    limit: int = 20,
    online_only: bool = False,
) -> str:
    """Search agent profiles by keyword or capability.

    query: free-text search
    capability: filter by capability (e.g. 'translator')
    limit: max results
    online_only: only return currently online agents
    """
    return _json(
        await _get_sdk().search_profiles(
            query=query, capability=capability, limit=limit, online_only=online_only
        )
    )


# ---------------------------------------------------------------------------
# Messaging (current-thread aware, auto-chaining)
# ---------------------------------------------------------------------------
@mcp.tool()
async def send_text(to: str, text: str, thread_id: str | None = None) -> str:
    """Send a text message on the current thread. Replies auto-chain.

    to: target agent (@username, acct_..., or capability:name)
    text: the message body
    thread_id: override the current thread (rarely needed)
    """
    global _last_message_id
    sdk = _get_sdk()
    tid = thread_id or _current_thread_id
    result = await sdk.send_text(
        to, text, thread_id=tid, parent_message_id=_last_message_id
    )
    _last_message_id = result.message_id
    return _json({"ok": True, "thread_id": tid, "message_id": result.message_id})


@mcp.tool()
async def ask_text(
    to: str, text: str, thread_id: str | None = None, timeout: float = 86400.0
) -> str:
    """Send a message on the current thread and wait for a reply. Replies auto-chain.

    to: target agent (@username, acct_..., or capability:name)
    text: the message body
    thread_id: override the current thread (rarely needed)
    timeout: seconds to wait for reply
    """
    global _last_message_id
    sdk = _get_sdk()
    tid = thread_id or _current_thread_id
    result = await sdk.ask_text(
        to, text, thread_id=tid, timeout=timeout, parent_message_id=_last_message_id
    )
    _last_message_id = result.message_id
    return _json({
        "ok": True,
        "thread_id": tid,
        "message_id": result.message_id,
        "reply_text": result.text,
        "reply_data": result.data,
    })


@mcp.tool()
async def delegate_task(
    to: str,
    text: str,
    title: str | None = None,
    parent_task_id: str | None = None,
    thread_id: str | None = None,
    wait: bool = False,
    timeout: float = 86400.0,
) -> str:
    """Assign background work to another Realm agent (job, not chat).

    to: target agent (@username, acct_..., or capability:name)
    text: concrete task instructions
    title: optional short label for the task
    parent_task_id: when re-delegating (Sandra→Daniela), pass your own task_id
    thread_id: optional audit thread; defaults to current thread
    wait: when true, block until the assigned agent returns a terminal task event
    timeout: seconds to wait when wait=true
    """
    global _last_message_id
    sdk = _get_sdk()
    store = _task_store
    if store is None:
        raise RuntimeError("task store not initialized")
    tid = thread_id or _current_thread_id or sdk.new_thread_id()
    task_id = new_task_id()
    try:
        # Delivery ACK is best-effort. Task state is authoritative in the registry.
        # False ACK timeouts were a major source of "broken" delegation UX.
        result = await sdk.delegate_task(
            to,
            text,
            task_id=task_id,
            title=title,
            parent_task_id=parent_task_id,
            thread_id=tid,
            parent_message_id=_last_message_id,
            require_delivery_ack=False,
        )
    except DeliveryAckTimeout as exc:
        registry_row: dict[str, Any] | None = None
        try:
            registry_status = await sdk.task_status(task_id, timeout=2.0)
            task = registry_status.get("task") if isinstance(registry_status.get("task"), dict) else registry_status
            if isinstance(task, dict) and str(task.get("task_id") or "") == task_id:
                registry_row = task
        except Exception:
            registry_row = None
        if registry_row is not None:
            await store.upsert(
                task_id,
                status=str(registry_row.get("status") or "assigned"),
                type=str(registry_row.get("type") or "task.assign"),
                target=to,
                title=title,
                text=text,
                payload=registry_row.get("payload"),
                thread_id=tid,
                message_id=str(registry_row.get("message_id") or "") or None,
                created_at=str(registry_row.get("created_at") or registry_row.get("sent_at") or ""),
                updated_at=str(registry_row.get("updated_at") or registry_row.get("received_at") or ""),
                delivery_ack="timeout",
            )
            if wait:
                return await await_task(task_id, timeout=timeout)
            return _json({"ok": True, "source": "registry", "delivery_ack": "timeout", **registry_row})
        row = await store.upsert(
            task_id,
            status="ack_timeout",
            type="task.assign",
            target=to,
            title=title,
            text=text,
            thread_id=tid,
            delivery_ack="timeout",
            error=str(exc),
        )
        return _json({"ok": False, "error": "delivery_ack_timeout", **row})
    _last_message_id = result.message_id
    row = await store.upsert(
        task_id,
        status="assigned",
        type="task.assign",
        target=to,
        title=title,
        text=text,
        payload=result.data,
        thread_id=tid,
        message_id=result.message_id,
        created_at=str((result.data or {}).get("created_at") if isinstance(result.data, dict) else ""),
        updated_at=str((result.data or {}).get("created_at") if isinstance(result.data, dict) else ""),
    )
    if wait:
        return await await_task(task_id, timeout=timeout)
    return _json({"ok": True, **row})


def _unwrap_task_status(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize registry envelopes to a flat task view."""
    task = row.get("task") if isinstance(row.get("task"), dict) else row
    if not isinstance(task, dict):
        return {"task_id": "", "status": "unknown"}
    out = dict(task)
    out.setdefault("task_id", str(row.get("task_id") or task.get("task_id") or ""))
    return out


@mcp.tool()
async def task_status(task_id: str) -> str:
    """Inspect one delegated background task by task_id.

    Returns status, latest_progress_text, terminal flag, parent_task_id when known.
    Poll this (or agent_status) instead of blocking forever on ask_text.
    """
    sdk = _get_sdk()
    store = _task_store
    if store is None:
        raise RuntimeError("task store not initialized")
    try:
        raw = await sdk.task_status(task_id)
        task = _unwrap_task_status(raw if isinstance(raw, dict) else {})
        return _json(
            {
                "ok": True,
                "source": "registry",
                "task_id": task.get("task_id") or task_id,
                "status": task.get("status"),
                "type": task.get("type"),
                "title": task.get("title"),
                "text": task.get("text"),
                "latest_progress_text": task.get("latest_progress_text"),
                "progress_event_count": task.get("progress_event_count"),
                "parent_task_id": task.get("parent_task_id"),
                "terminal": task.get("terminal"),
                "updated_at": task.get("updated_at"),
                "task": task,
            }
        )
    except Exception:
        pass
    row = await store.get(task_id)
    if row is None:
        return _json({"ok": False, "error": "task_not_found", "task_id": task_id})
    return _json({"ok": True, "source": "local_ledger", **row})


@mcp.tool()
async def report_progress(
    to: str,
    task_id: str,
    text: str,
    percent: float | None = None,
    phase: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Report live progress on a task so coordinators can see what you are doing.

    to: coordinator or interested party (@username / acct_...)
    task_id: the task you are working on
    text: short human-readable update (what you are doing now)
    percent: optional 0-100 progress
    phase: optional phase label (e.g. researching, coding, reviewing)
    """
    global _last_message_id
    sdk = _get_sdk()
    tid = thread_id or _current_thread_id
    result = await sdk.report_progress(
        to,
        task_id,
        text,
        thread_id=tid,
        parent_message_id=_last_message_id,
        percent=percent,
        phase=phase,
    )
    _last_message_id = result.message_id
    return _json(
        {
            "ok": True,
            "task_id": task_id,
            "thread_id": tid,
            "message_id": result.message_id,
            "text": text,
            "percent": percent,
            "phase": phase,
        }
    )


@mcp.tool()
async def list_tasks(
    limit: int = 20,
    assignee: str | None = None,
    coordinator: str | None = None,
    parent_task_id: str | None = None,
    status: str | None = None,
) -> str:
    """List delegated tasks. Filter by assignee/coordinator (@user or acct_), parent_task_id, status."""
    sdk = _get_sdk()
    store = _task_store
    if store is None:
        raise RuntimeError("task store not initialized")

    async def _resolve_account(ref: str | None) -> str | None:
        value = str(ref or "").strip()
        if not value:
            return None
        if value.startswith("acct_") or value.lower().startswith("account:"):
            return value.split(":", 1)[-1].strip()
        try:
            profile = await sdk.get_profile(value if value.startswith("@") else f"@{value}")
            account_id = str(profile.get("account_id") or "").strip()
            return account_id or None
        except Exception:
            return None

    try:
        assignee_account_id = await _resolve_account(assignee)
        coordinator_account_id = await _resolve_account(coordinator)
        return _json(
            {
                "ok": True,
                "source": "registry",
                "tasks": await sdk.list_tasks(
                    assignee_account_id=assignee_account_id,
                    coordinator_account_id=coordinator_account_id,
                    parent_task_id=parent_task_id,
                    status=status,
                    limit=limit,
                ),
            }
        )
    except Exception:
        return _json({"ok": True, "source": "local_ledger", "tasks": await store.list(limit=limit)})


@mcp.tool()
async def await_task(task_id: str, timeout: float = 86400.0) -> str:
    """Wait until a delegated task reports completed, blocked, or failed."""
    sdk = _get_sdk()
    store = _task_store
    condition = _task_condition
    if store is None or condition is None:
        raise RuntimeError("task store not initialized")
    wait_budget, capped = _blocking_wait_budget(timeout)
    deadline = asyncio.get_running_loop().time() + wait_budget
    while True:
        try:
            registry_row = await sdk.task_status(task_id, timeout=2.0)
            task = registry_row.get("task") if isinstance(registry_row.get("task"), dict) else registry_row
            status = str(task.get("status") or "") if isinstance(task, dict) else ""
            payload_type = str(task.get("type") or "") if isinstance(task, dict) else ""
            if payload_type in TERMINAL_TASK_TYPES or status in {"completed", "blocked", "failed"}:
                return _json({"ok": status == "completed", "source": "registry", **task})
        except Exception:
            pass
        row = await store.get(task_id)
        if row is None:
            row = {"task_id": task_id, "status": "unknown"}
        local_payload_type = str(row.get("type") or "")
        local_status = str(row.get("status") or "")
        if local_payload_type in TERMINAL_TASK_TYPES or local_status in {"completed", "blocked", "failed"}:
            return _json({"ok": local_status == "completed", "source": "local_ledger", **row})
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if capped:
                return _json(
                    {
                        "ok": True,
                        "terminal": False,
                        "source": "wait_budget",
                        "error": "task_still_running",
                        "message": (
                            "Task is still running. Poll await_task or task_status "
                            "with this task_id."
                        ),
                        "requested_timeout_seconds": float(timeout),
                        "waited_seconds": wait_budget,
                        "max_blocking_wait_seconds": MAX_BLOCKING_WAIT_SECONDS,
                        **row,
                    }
                )
            return _json({"ok": False, "error": "task_timeout", **row})
        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Thread browsing
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_thread_messages(
    thread_id: str | None = None, limit: int = 50, cursor: str | None = None
) -> str:
    """Read messages from a thread. Defaults to the current thread.

    thread_id: defaults to current thread
    limit: max messages
    cursor: pagination cursor from previous response
    """
    tid = thread_id or _current_thread_id
    return _json(await _get_sdk().get_thread_messages(thread_id=tid, limit=limit, cursor=cursor))


@mcp.tool()
async def list_threads(
    participant: str | None = None, query: str = "", limit: int = 20
) -> str:
    """List conversation threads you're part of. Current thread is marked.

    participant: filter by participant (@username or acct_...)
    query: free-text search
    limit: max results
    """
    sdk = _get_sdk()
    pa = None
    pu = None
    if participant:
        if participant.startswith("@"):
            pu = participant
        else:
            pa = participant
    results = await sdk.list_threads(
        participant_account_id=pa, participant_username=pu, query=query, limit=limit
    )
    for r in results:
        r["_current"] = r.get("thread_id") == _current_thread_id
    return _json(results)


@mcp.tool()
async def search_messages(
    thread_id: str | None = None,
    from_account_id: str | None = None,
    to_account_id: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> str:
    """Search messages across threads with filters.

    thread_id: optional thread to scope search to
    from_account_id: sender filter
    to_account_id: recipient filter
    kind: message kind filter (direct, request, reply, stream)
    limit: max results
    cursor: pagination cursor
    """
    return _json(
        await _get_sdk().search_messages(
            thread_id=thread_id,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            kind=kind,
            limit=limit,
            cursor=cursor,
        )
    )


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
@mcp.tool()
async def thread_status(thread_id: str | None = None) -> str:
    """Check the compaction/budget status of a thread. Defaults to current.

    thread_id: defaults to current thread
    """
    tid = thread_id or _current_thread_id
    return _json(await _get_sdk().thread_status(tid))


@mcp.tool()
async def registry_metrics() -> str:
    """Get network health stats: agent count, message throughput, uptime."""
    return _json(await _get_sdk().registry_metrics())


@mcp.tool()
async def get_agent_state(agent: str) -> str:
    """Read a Realm agent's current task state.

    Tries the local state file first. If not found, queries the agent
    over the Realm network with a ``STATE`` request.

    agent: agent name (e.g. m4-dl, eng-m2)
    """
    state_dir = os.getenv(
        "REALM_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".local", "share"),
    )
    state_path = os.path.join(
        os.path.expanduser(state_dir), agent, "state", f"{agent}.json"
    )

    # -- try local file first -------------------------------------------------
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
            data["source"] = "local_file"
            return _json(data)
    except FileNotFoundError:
        pass
    except Exception as exc:
        return _json({"error": str(exc), "source": "local_file_error", "path": state_path})

    # -- fallback: ask the agent over Realm -----------------------------------
    sdk = _get_sdk()
    try:
        result = await asyncio.wait_for(
            sdk.ask_text(f"@{agent}", "STATE", timeout=10.0),
            timeout=15.0,
        )
        reply = result.data if isinstance(result.data, dict) else {}
        text = reply.get("text") or result.text or ""
        parsed = json.loads(text) if text else {}
        if isinstance(parsed, dict) and parsed.get("type") == "state":
            data = parsed.get("data", {})
            data["source"] = "agent_state_request"
            return _json(data)
        return _json({"error": "unexpected STATE reply", "raw": text})
    except asyncio.TimeoutError:
        return _json({"error": f"STATE request to {agent} timed out"})
    except Exception as exc:
        return _json({"error": str(exc), "source": "agent_state_request_error"})


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
