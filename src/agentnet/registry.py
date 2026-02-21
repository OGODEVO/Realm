"""Registry request helpers used by agents and CLI."""

from __future__ import annotations

from typing import Any

from nats.aio.client import Client as NATS
from nats.errors import NoServersError, TimeoutError

from agentnet.schema import AgentInfo
from agentnet.subjects import REGISTRY_LIST_SUBJECT
from agentnet.utils import decode_json, encode_json


async def list_online_agents(nats_url: str = "nats://localhost:4222", timeout: float = 2.0) -> list[AgentInfo]:
    nc = NATS()
    try:
        await nc.connect(
            servers=[nats_url],
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=timeout,
        )
    except (NoServersError, OSError) as exc:
        raise RuntimeError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await list_online_agents_with_client(nc, timeout=timeout)
    finally:
        await nc.drain()


async def list_online_agents_with_client(nc: NATS, timeout: float = 2.0) -> list[AgentInfo]:
    try:
        response = await nc.request(REGISTRY_LIST_SUBJECT, encode_json({}), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError("Registry did not respond to registry.list") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        return []

    raw_agents = data.get("agents") or []
    if not isinstance(raw_agents, list):
        return []

    agents: list[AgentInfo] = []
    for entry in raw_agents:
        if isinstance(entry, dict):
            info = AgentInfo.from_dict(entry)
            if info.agent_id:
                agents.append(info)
    return agents
