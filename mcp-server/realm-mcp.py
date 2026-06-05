#!/usr/bin/env python3
"""Realm MCP Server — exposes AgentNet as MCP tools for AI agents.

Every server instance maintains a *current thread*. All sends/replies
default to that thread and auto-chain parent_message_id. The agent
feels like it's having one ongoing conversation.

Tools:
  current_thread         — see which thread is active
  new_thread [name]      — start a fresh thread (switches to it)
  switch_thread <id>     — switch to another thread by ID
  list_online            — who's on the network
  get_profile <target>   — look up an agent
  search_profiles <...>  — find agents by keyword/capability
  send_text <to> <text>  — fire-and-forget message (current thread)
  ask_text <to> <text>   — send and wait for reply (current thread)
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

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from agentnet.sdk import AgentSDK
from mcp.server.fastmcp import FastMCP

NATS_URL = os.getenv("REALM_NATS_URL", "nats://agentnet_secret_token@localhost:4222")
AGENT_NAME = os.getenv("REALM_AGENT_NAME", "medusa-bridge")

_sdk: AgentSDK | None = None
_current_thread_id: str | None = None
_last_message_id: str | None = None


def _get_sdk() -> AgentSDK:
    if _sdk is None:
        raise RuntimeError("SDK not connected — lifespan not yet started")
    return _sdk


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _sdk, _current_thread_id, _last_message_id
    try:
        _sdk = AgentSDK(
            agent_id=f"mcp_{AGENT_NAME}",
            name=AGENT_NAME,
            username=AGENT_NAME,
            capabilities=["mcp-bridge", "realm-tools"],
            nats_url=NATS_URL,
            metadata={"kind": "mcp-server", "hostname": os.uname().nodename},
        )
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


mcp = FastMCP(
    name="realm-mcp",
    instructions=(
        "Realm / AgentNet MCP bridge. "
        "You are always in a conversation thread — send_text and ask_text default to it. "
        "Replies auto-chain. Use new_thread() to start a fresh conversation."
    ),
    lifespan=lifespan,
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
    """List all agents currently online on the Realm network."""
    agents = await _get_sdk().list_online()
    return _json([a.to_dict() for a in agents])


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
    to: str, text: str, thread_id: str | None = None, timeout: float = 60.0
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
