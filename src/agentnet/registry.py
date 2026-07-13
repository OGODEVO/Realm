"""Registry request helpers used by agents and CLI."""

from __future__ import annotations

from typing import Any

from nats.aio.client import Client as NATS
from nats.errors import NoServersError, TimeoutError

from agentnet.config import DEFAULT_NATS_URL
from agentnet.exceptions import (
    ConnectionError,
    registry_protocol_error,
    registry_remote_error,
    registry_timeout,
)
from agentnet.schema import AgentInfo
from agentnet.subjects import (
    REGISTRY_METRICS_SUBJECT,
    REGISTRY_MESSAGE_SEARCH_SUBJECT,
    REGISTRY_LIST_SUBJECT,
    REGISTRY_PROFILE_SUBJECT,
    REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
    REGISTRY_RESOLVE_KEY_SUBJECT,
    REGISTRY_SEARCH_SUBJECT,
    REGISTRY_TASK_LIST_SUBJECT,
    REGISTRY_TASK_STATUS_SUBJECT,
    REGISTRY_THREAD_LIST_SUBJECT,
    REGISTRY_THREAD_MESSAGES_SUBJECT,
    REGISTRY_THREAD_STATUS_SUBJECT,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await list_online_agents_with_client(nc, timeout=timeout)
    finally:
        await nc.drain()


async def list_online_agents_with_client(nc: NATS, timeout: float = 2.0) -> list[AgentInfo]:
    try:
        response = await nc.request(REGISTRY_LIST_SUBJECT, encode_json({}), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.list") from exc

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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
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
        raise registry_timeout("registry.resolve_account") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.resolve_account", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.resolve_account", data.get("error") or "resolve_account_failed")

    account_id = str(data.get("account_id") or "")
    resolved_username = str(data.get("username") or "")
    if not account_id:
        raise registry_protocol_error("registry.resolve_account", "missing account_id")
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
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
        raise registry_timeout("registry.resolve_key") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.resolve_key", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.resolve_key", data.get("error") or "resolve_key_failed")
    public_key = str(data.get("public_key") or "")
    if not public_key:
        raise registry_protocol_error("registry.resolve_key", "missing public_key")
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
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
        raise registry_timeout("registry.search") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.search", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.search", data.get("error") or "search_failed")
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
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
        raise registry_timeout("registry.profile") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.profile", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.profile", data.get("error") or "profile_failed")
    profile = data.get("profile")
    if not isinstance(profile, dict):
        raise registry_protocol_error("registry.profile", "missing profile")
    return profile


async def get_thread_status(
    nats_url: str,
    *,
    thread_id: str,
    soft_limit_tokens: int | None = None,
    hard_limit_tokens: int | None = None,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await get_thread_status_with_client(
            nc,
            thread_id=thread_id,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            timeout=timeout,
        )
    finally:
        await nc.drain()


async def get_thread_status_with_client(
    nc: NATS,
    *,
    thread_id: str,
    soft_limit_tokens: int | None = None,
    hard_limit_tokens: int | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        raise ValueError("thread_id is required")

    _ = soft_limit_tokens, hard_limit_tokens
    payload: dict[str, Any] = {"thread_id": normalized_thread_id}

    try:
        response = await nc.request(REGISTRY_THREAD_STATUS_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.thread_status") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.thread_status", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.thread_status", data.get("error") or "thread_status_failed")
    return data


async def list_threads(
    nats_url: str,
    *,
    participant_account_id: str | None = None,
    participant_username: str | None = None,
    query: str = "",
    limit: int = 20,
    soft_limit_tokens: int | None = None,
    hard_limit_tokens: int | None = None,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await list_threads_with_client(
            nc,
            participant_account_id=participant_account_id,
            participant_username=participant_username,
            query=query,
            limit=limit,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            timeout=timeout,
        )
    finally:
        await nc.drain()


async def list_threads_with_client(
    nc: NATS,
    *,
    participant_account_id: str | None = None,
    participant_username: str | None = None,
    query: str = "",
    limit: int = 20,
    soft_limit_tokens: int | None = None,
    hard_limit_tokens: int | None = None,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    _ = soft_limit_tokens, hard_limit_tokens
    safe_limit = max(1, min(int(limit), 100))
    payload: dict[str, Any] = {
        "query": str(query or "").strip(),
        "limit": safe_limit,
    }
    if participant_account_id:
        payload["participant_account_id"] = participant_account_id.strip()
    if participant_username:
        payload["participant_username"] = participant_username.strip().lower().lstrip("@")

    try:
        response = await nc.request(REGISTRY_THREAD_LIST_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.thread_list") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.thread_list", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.thread_list", data.get("error") or "thread_list_failed")
    results = data.get("results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


async def get_thread_messages(
    nats_url: str,
    *,
    thread_id: str,
    limit: int = 50,
    cursor: str | None = None,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await get_thread_messages_with_client(
            nc,
            thread_id=thread_id,
            limit=limit,
            cursor=cursor,
            timeout=timeout,
        )
    finally:
        await nc.drain()


async def get_thread_messages_with_client(
    nc: NATS,
    *,
    thread_id: str,
    limit: int = 50,
    cursor: str | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        raise ValueError("thread_id is required")
    payload: dict[str, Any] = {
        "thread_id": normalized_thread_id,
        "limit": max(1, min(int(limit), 200)),
    }
    normalized_cursor = str(cursor or "").strip()
    if normalized_cursor:
        payload["cursor"] = normalized_cursor
    try:
        response = await nc.request(REGISTRY_THREAD_MESSAGES_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.thread_messages") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.thread_messages", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.thread_messages", data.get("error") or "thread_messages_failed")
    return data


async def search_messages(
    nats_url: str,
    *,
    thread_id: str | None = None,
    from_account_id: str | None = None,
    to_account_id: str | None = None,
    kind: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await search_messages_with_client(
            nc,
            thread_id=thread_id,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            kind=kind,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            cursor=cursor,
            timeout=timeout,
        )
    finally:
        await nc.drain()


async def search_messages_with_client(
    nc: NATS,
    *,
    thread_id: str | None = None,
    from_account_id: str | None = None,
    to_account_id: str | None = None,
    kind: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "limit": max(1, min(int(limit), 200)),
    }
    if thread_id:
        payload["thread_id"] = str(thread_id).strip()
    if from_account_id:
        payload["from_account_id"] = str(from_account_id).strip()
    if to_account_id:
        payload["to_account_id"] = str(to_account_id).strip()
    if kind:
        payload["kind"] = str(kind).strip()
    if from_ts:
        payload["from_ts"] = str(from_ts).strip()
    if to_ts:
        payload["to_ts"] = str(to_ts).strip()
    normalized_cursor = str(cursor or "").strip()
    if normalized_cursor:
        payload["cursor"] = normalized_cursor
    try:
        response = await nc.request(REGISTRY_MESSAGE_SEARCH_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.message_search") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.message_search", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.message_search", data.get("error") or "message_search_failed")
    return data


async def get_task_status(
    nats_url: str,
    *,
    task_id: str,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await get_task_status_with_client(nc, task_id=task_id, timeout=timeout)
    finally:
        await nc.drain()


async def get_task_status_with_client(
    nc: NATS,
    *,
    task_id: str,
    timeout: float = 2.0,
) -> dict[str, Any]:
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        raise ValueError("task_id is required")
    try:
        response = await nc.request(
            REGISTRY_TASK_STATUS_SUBJECT,
            encode_json({"task_id": normalized_task_id}),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise registry_timeout("registry.task_status") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.task_status", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.task_status", data.get("error") or "task_status_failed")
    return data


async def list_tasks(
    nats_url: str,
    *,
    assignee_account_id: str | None = None,
    coordinator_account_id: str | None = None,
    status: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 20,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await list_tasks_with_client(
            nc,
            assignee_account_id=assignee_account_id,
            coordinator_account_id=coordinator_account_id,
            status=status,
            parent_task_id=parent_task_id,
            limit=limit,
            timeout=timeout,
        )
    finally:
        await nc.drain()


async def list_tasks_with_client(
    nc: NATS,
    *,
    assignee_account_id: str | None = None,
    coordinator_account_id: str | None = None,
    status: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 20,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
    if assignee_account_id:
        payload["assignee_account_id"] = str(assignee_account_id).strip()
    if coordinator_account_id:
        payload["coordinator_account_id"] = str(coordinator_account_id).strip()
    if status:
        payload["status"] = str(status).strip()
    if parent_task_id:
        payload["parent_task_id"] = str(parent_task_id).strip()
    try:
        response = await nc.request(REGISTRY_TASK_LIST_SUBJECT, encode_json(payload), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.task_list") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.task_list", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.task_list", data.get("error") or "task_list_failed")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [item for item in tasks if isinstance(item, dict)]


def _normalize_status_target(target: str) -> tuple[str, str]:
    normalized = str(target or "").strip()
    if not normalized:
        raise ValueError("target is required")
    lowered = normalized.lower()
    if lowered.startswith("account:") or normalized.startswith("acct_"):
        account_id = normalized.split(":", 1)[-1].strip() if ":" in normalized else normalized
        if not account_id:
            raise ValueError("account target cannot be empty")
        return "account", account_id
    username = normalized[1:] if normalized.startswith("@") else normalized
    username = username.strip()
    if not username:
        raise ValueError("username target cannot be empty")
    return "username", username


def _agent_status_summary(
    *,
    label: str,
    online: bool,
    active_tasks: list[dict[str, Any]],
) -> str:
    if not online:
        return f"{label} is offline"
    if not active_tasks:
        return f"{label} is online and idle"
    top = active_tasks[0]
    title = str(top.get("title") or top.get("assigned_text") or top.get("task_id") or "a task").strip()
    status = str(top.get("status") or "working").strip()
    progress = str(top.get("latest_progress_text") or "").strip()
    if progress:
        return f"{label} is {status} on {title}: {progress}"
    return f"{label} is {status} on {title}"


async def get_agent_status(
    nats_url: str,
    target: str,
    *,
    limit: int = 10,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """What is this agent doing? Presence + active/recent tasks + one-line summary."""
    nc = NATS()
    try:
        await nc.connect(
            servers=[nats_url],
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=timeout,
        )
    except (NoServersError, OSError) as exc:
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await get_agent_status_with_client(nc, target, limit=limit, timeout=timeout)
    finally:
        await nc.drain()


async def get_agent_status_with_client(
    nc: NATS,
    target: str,
    *,
    limit: int = 10,
    timeout: float = 2.0,
) -> dict[str, Any]:
    kind, value = _normalize_status_target(target)
    account_id: str | None = None
    username: str | None = None
    display = f"@{value}" if kind == "username" else value

    if kind == "account":
        account_id = value
    else:
        username = value
        try:
            account_id, username = await resolve_account_by_username_with_client(
                nc, username=username, timeout=timeout
            )
            display = f"@{username}"
        except Exception:
            account_id = None

    online_agents = await list_online_agents_with_client(nc, timeout=timeout)
    matching = []
    for agent in online_agents:
        if account_id and agent.account_id == account_id:
            matching.append(agent)
        elif username and (agent.username or "").lower() == username.lower():
            matching.append(agent)
            if not account_id and agent.account_id:
                account_id = agent.account_id

    online = bool(matching)
    session_count = 0
    if matching:
        session_count = int((matching[0].metadata or {}).get("session_count") or len(matching))
        if not account_id:
            account_id = matching[0].account_id
        if not username:
            username = matching[0].username

    recent_tasks: list[dict[str, Any]] = []
    if account_id:
        recent_tasks = await list_tasks_with_client(
            nc,
            assignee_account_id=account_id,
            limit=max(1, min(int(limit), 50)),
            timeout=timeout,
        )
    active_tasks = [task for task in recent_tasks if not bool(task.get("terminal"))]
    summary = _agent_status_summary(label=display, online=online, active_tasks=active_tasks)
    return {
        "ok": True,
        "target": display,
        "username": username,
        "account_id": account_id,
        "online": online,
        "session_count": session_count,
        "active_tasks": active_tasks,
        "recent_tasks": recent_tasks,
        "summary": summary,
    }


async def get_registry_metrics(
    nats_url: str,
    *,
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
        raise ConnectionError(f"Cannot connect to NATS at {nats_url}. Is it running?") from exc
    try:
        return await get_registry_metrics_with_client(nc, timeout=timeout)
    finally:
        await nc.drain()


async def get_registry_metrics_with_client(
    nc: NATS,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    try:
        response = await nc.request(REGISTRY_METRICS_SUBJECT, encode_json({}), timeout=timeout)
    except TimeoutError as exc:
        raise registry_timeout("registry.metrics") from exc

    data: Any = decode_json(response.data)
    if not isinstance(data, dict):
        raise registry_protocol_error("registry.metrics", "response must be an object")
    if "error" in data:
        raise registry_remote_error("registry.metrics", data.get("error") or "metrics_failed")
    return data
