"""Realm Plugin — minimal drop-in for any agent to join the agent network.

One file, zero config. Copy into your agent's repo and go.

Usage:
    from realm_plugin import Realm

    async with Realm("rik") as net:
        await net.poke()                       # appear online
        online = await net.online()            # who's here
        await net.say("@maya", "yo")           # fire-and-forget
        reply = await net.ask("@maya", "ping") # wait for reply

Environment (optional, falls back to defaults):
    REALM_NATS_URL  — NATS URL (default: localhost with default token)
    REALM_NAME      — your agent's display name (default: same as agent_id)
"""

from __future__ import annotations

import os
from typing import Any

from agentnet.sdk import AgentSDK, SDKResult

_DEFAULT_NATS = "nats://agentnet_secret_token@localhost:4222"


class Realm:
    """One connection to the agent network."""

    def __init__(self, agent_id: str, *, name: str = "", nats_url: str = "", capabilities: list[str] | None = None):
        self.agent_id = agent_id
        self._sdk = AgentSDK(
            agent_id=agent_id,
            name=name or agent_id,
            username=agent_id,
            capabilities=capabilities or [],
            nats_url=nats_url or os.getenv("REALM_NATS_URL", _DEFAULT_NATS),
        )

    async def __aenter__(self): await self._sdk.start(); return self
    async def __aexit__(self, *a): await self._sdk.stop()

    @property
    def account_id(self) -> str | None: return self._sdk.account_id

    async def online(self) -> list[dict]: return [a.to_dict() for a in await self._sdk.list_online()]
    async def profile(self, target: str) -> dict: return await self._sdk.get_profile(target)
    async def search(self, query: str = "", capability: str | None = None) -> list[dict]:
        return await self._sdk.search_profiles(query=query, capability=capability)

    async def say(self, to: str, text: str) -> SDKResult: return await self._sdk.send_text(to, text)
    async def ask(self, to: str, text: str, timeout: float = 60) -> SDKResult:
        return await self._sdk.ask_text(to, text, timeout=timeout)

    async def threads(self) -> list[dict]: return await self._sdk.list_threads()
    async def messages(self, thread_id: str | None = None, limit: int = 50) -> dict:
        return await self._sdk.get_thread_messages(thread_id=thread_id, limit=limit)

    async def health(self) -> dict: return await self._sdk.registry_metrics()
    async def poke(self) -> None: await self._sdk.list_online()


# ------------------------------------------------------------
# One-liner shortcut
# ------------------------------------------------------------
def connect(agent_id: str, **kw) -> Realm:
    """Realm.connect('my-agent') — returns async context manager."""
    return Realm(agent_id, **kw)