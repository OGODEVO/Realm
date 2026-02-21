"""Registry service that tracks online agents via NATS subjects."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from agentnet.schema import AgentInfo
from agentnet.subjects import (
    REGISTRY_GOODBYE_SUBJECT,
    REGISTRY_HELLO_SUBJECT,
    REGISTRY_LIST_SUBJECT,
)
from agentnet.utils import decode_json, encode_json, utc_now_iso


class RegistryService:
    def __init__(self, nats_url: str, ttl_seconds: float = 40.0, gc_interval_seconds: float = 5.0) -> None:
        self.nats_url = nats_url
        self.ttl_seconds = ttl_seconds
        self.gc_interval_seconds = gc_interval_seconds

        self._nc: NATS | None = None
        self._gc_task: asyncio.Task[None] | None = None
        self._agents: dict[str, AgentInfo] = {}
        self._last_seen_by_id: dict[str, float] = {}

    async def start(self) -> None:
        self._nc = NATS()
        await self._nc.connect(servers=[self.nats_url], name="agentnet-registry")

        await self._nc.subscribe(REGISTRY_HELLO_SUBJECT, cb=self._on_hello)
        await self._nc.subscribe(REGISTRY_GOODBYE_SUBJECT, cb=self._on_goodbye)
        await self._nc.subscribe(REGISTRY_LIST_SUBJECT, cb=self._on_list)

        self._gc_task = asyncio.create_task(self._gc_loop(), name="agentnet-registry-gc")
        print(f"registry ready: nats={self.nats_url} ttl={self.ttl_seconds}s")

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

    async def _on_hello(self, msg: Msg) -> None:
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            return

        agent = AgentInfo.from_dict(data)
        if not agent.agent_id:
            return

        agent.last_seen = utc_now_iso()
        self._agents[agent.agent_id] = agent
        self._last_seen_by_id[agent.agent_id] = time.monotonic()

    async def _on_goodbye(self, msg: Msg) -> None:
        data = decode_json(msg.data)
        if not isinstance(data, dict):
            return

        agent_id = str(data.get("agent_id") or "")
        if not agent_id:
            return

        self._agents.pop(agent_id, None)
        self._last_seen_by_id.pop(agent_id, None)

    async def _on_list(self, msg: Msg) -> None:
        if not msg.reply or not self._nc:
            return

        self._evict_stale()

        payload = {
            "generated_at": utc_now_iso(),
            "agents": [agent.to_dict() for agent in sorted(self._agents.values(), key=lambda item: item.agent_id)],
        }
        await self._nc.publish(msg.reply, encode_json(payload))

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(self.gc_interval_seconds)
            self._evict_stale()

    def _evict_stale(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        stale_ids = [agent_id for agent_id, seen in self._last_seen_by_id.items() if seen < cutoff]
        for agent_id in stale_ids:
            self._last_seen_by_id.pop(agent_id, None)
            self._agents.pop(agent_id, None)


async def amain() -> None:
    nats_url = os.getenv("NATS_URL", "nats://nats:4222")
    ttl_seconds = float(os.getenv("AGENT_TTL_SECONDS", "40"))
    gc_interval = float(os.getenv("GC_INTERVAL_SECONDS", "5"))

    service = RegistryService(nats_url=nats_url, ttl_seconds=ttl_seconds, gc_interval_seconds=gc_interval)
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
