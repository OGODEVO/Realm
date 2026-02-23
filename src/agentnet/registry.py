"""Registry request helpers used by agents and CLI."""

from __future__ import annotations

from typing import Any

from nats.aio.client import Client as NATS
from nats.errors import NoServersError, TimeoutError

from agentnet.config import DEFAULT_NATS_URL
from agentnet.schema import AgentInfo
from agentnet.subjects import REGISTRY_LIST_SUBJECT, REGISTRY_RESOLVE_ACCOUNT_SUBJECT
from agentnet.utils import decode_json, encode_json


async def list_online_agents(nats_url: str = DEFAULT_NATS_URL, timeout: float = 2.0) -> list[AgentInfo]:
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


async def resolve_account_by_username(nats_url: str, username: str, timeout: float = 2.0) -> tuple[str, str]:
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
        return await resolve_account_by_username_with_client(nc, username=username, timeout=timeout)
    finally:
        await nc.drain()


async def resolve_account_by_username_with_client(nc: NATS, username: str, timeout: float = 2.0) -> tuple[str, str]:
    target = username.strip().lower().lstrip("@")
    if not target:
        raise ValueError("username is required")
    try:
        response = await nc.request(
            REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
            encode_json({"username": target}),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise RuntimeError("Registry did not respond to registry.resolve_account") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise RuntimeError("registry.resolve_account response must be an object")
    if "error" in data:
        raise RuntimeError(str(data.get("error") or "resolve_account_failed"))

    account_id = str(data.get("account_id") or "")
    resolved_username = str(data.get("username") or "")
    if not account_id:
        raise RuntimeError("registry.resolve_account missing account_id")
    if not resolved_username:
        resolved_username = target
    return account_id, resolved_username
