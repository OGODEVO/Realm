"""Registry request helpers used by agents and CLI."""

from __future__ import annotations

from typing import Any

from nats.aio.client import Client as NATS
from nats.errors import NoServersError, TimeoutError

from agentnet.config import DEFAULT_NATS_URL
from agentnet.schema import AgentInfo
from agentnet.subjects import (
    REGISTRY_LIST_SUBJECT,
    REGISTRY_PROFILE_SUBJECT,
    REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
    REGISTRY_RESOLVE_KEY_SUBJECT,
    REGISTRY_SEARCH_SUBJECT,
)
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


async def resolve_dev_public_key_by_account(
    nats_url: str,
    account_id: str,
    timeout: float = 2.0,
) -> str:
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
        return await resolve_dev_public_key_by_account_with_client(nc, account_id=account_id, timeout=timeout)
    finally:
        await nc.drain()


async def resolve_dev_public_key_by_account_with_client(
    nc: NATS,
    account_id: str,
    timeout: float = 2.0,
) -> str:
    target = account_id.strip()
    if not target:
        raise ValueError("account_id is required")
    try:
        response = await nc.request(
            REGISTRY_RESOLVE_KEY_SUBJECT,
            encode_json({"account_id": target}),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise RuntimeError("Registry did not respond to registry.resolve_key") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise RuntimeError("registry.resolve_key response must be an object")
    if "error" in data:
        raise RuntimeError(str(data.get("error") or "resolve_key_failed"))
    public_key = str(data.get("public_key") or "")
    if not public_key:
        raise RuntimeError("registry.resolve_key missing public_key")
    return public_key


async def search_profiles(
    nats_url: str,
    *,
    query: str = "",
    capability: str | None = None,
    limit: int = 20,
    online_only: bool = False,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
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
        return await search_profiles_with_client(
            nc,
            query=query,
            capability=capability,
            limit=limit,
            online_only=online_only,
            timeout=timeout,
        )
    finally:
        await nc.drain()


async def search_profiles_with_client(
    nc: NATS,
    *,
    query: str = "",
    capability: str | None = None,
    limit: int = 20,
    online_only: bool = False,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 100))
    payload = {
        "query": str(query or "").strip(),
        "capability": str(capability or "").strip() or None,
        "limit": safe_limit,
        "online_only": bool(online_only),
    }
    try:
        response = await nc.request(REGISTRY_SEARCH_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError("Registry did not respond to registry.search") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise RuntimeError("registry.search response must be an object")
    if "error" in data:
        raise RuntimeError(str(data.get("error") or "search_failed"))
    results = data.get("results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


async def get_profile(
    nats_url: str,
    *,
    account_id: str | None = None,
    username: str | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
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
        return await get_profile_with_client(nc, account_id=account_id, username=username, timeout=timeout)
    finally:
        await nc.drain()


async def get_profile_with_client(
    nc: NATS,
    *,
    account_id: str | None = None,
    username: str | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if account_id:
        payload["account_id"] = account_id.strip()
    if username:
        payload["username"] = username.strip().lower().lstrip("@")
    if not payload:
        raise ValueError("account_id or username is required")
    try:
        response = await nc.request(REGISTRY_PROFILE_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError("Registry did not respond to registry.profile") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise RuntimeError("registry.profile response must be an object")
    if "error" in data:
        raise RuntimeError(str(data.get("error") or "profile_failed"))
    profile = data.get("profile")
    if not isinstance(profile, dict):
        raise RuntimeError("registry.profile missing profile")
    return profile
