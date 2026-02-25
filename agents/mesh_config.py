from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class MeshDefaults:
    nats_url_env: str = "NATS_URL"
    max_history_messages: int = 24
    request_timeout_seconds: float = 60.0
    context_timezone: str = "America/Chicago"


@dataclass(slots=True)
class AgentModelConfig:
    provider: str
    model: str
    api_key_env: str
    base_url_env: str
    temperature: float = 0.2
    max_tokens: int = 400


@dataclass(slots=True)
class AgentProfile:
    key: str
    agent_id: str
    username: str
    name: str
    system_prompt_path: Path
    allowed_tools: list[str]
    model: AgentModelConfig


@dataclass(slots=True)
class MeshConfig:
    defaults: MeshDefaults
    agents: list[AgentProfile]


def _as_str(data: dict[str, Any], key: str) -> str:
    raw = data.get(key)
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"Missing required config field: {key}")
    return value


def load_mesh_config(path: str | Path) -> MeshConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Mesh config root must be a mapping.")

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        defaults_raw = {}
    defaults = MeshDefaults(
        nats_url_env=str(defaults_raw.get("nats_url_env") or "NATS_URL"),
        max_history_messages=max(1, int(defaults_raw.get("max_history_messages") or 24)),
        request_timeout_seconds=max(1.0, float(defaults_raw.get("request_timeout_seconds") or 60.0)),
        context_timezone=str(defaults_raw.get("context_timezone") or "America/Chicago"),
    )

    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ValueError("Config must include a non-empty agents list.")

    profiles: list[AgentProfile] = []
    seen_keys: set[str] = set()
    seen_usernames: set[str] = set()
    for item in agents_raw:
        if not isinstance(item, dict):
            raise ValueError("Each agent entry must be a mapping.")

        key = _as_str(item, "key")
        if key in seen_keys:
            raise ValueError(f"Duplicate agent key in config: {key}")
        seen_keys.add(key)

        username = _as_str(item, "username")
        if username.lower() in seen_usernames:
            raise ValueError(f"Duplicate username in config: {username}")
        seen_usernames.add(username.lower())

        model_raw = item.get("model_config")
        if not isinstance(model_raw, dict):
            raise ValueError(f"Agent '{key}' is missing model_config mapping.")
        model_config = AgentModelConfig(
            provider=_as_str(model_raw, "provider").lower(),
            model=_as_str(model_raw, "model"),
            api_key_env=_as_str(model_raw, "api_key_env"),
            base_url_env=_as_str(model_raw, "base_url_env"),
            temperature=float(model_raw.get("temperature") if model_raw.get("temperature") is not None else 0.2),
            max_tokens=max(1, int(model_raw.get("max_tokens") or 400)),
        )

        prompt_path = Path(_as_str(item, "system_prompt_path"))
        if not prompt_path.is_absolute():
            prompt_path = (config_path.parent.parent / prompt_path).resolve()

        allowed_tools_raw = item.get("allowed_tools") or []
        if not isinstance(allowed_tools_raw, list):
            raise ValueError(f"Agent '{key}' allowed_tools must be a list.")
        allowed_tools = [str(tool).strip() for tool in allowed_tools_raw if str(tool).strip()]

        profiles.append(
            AgentProfile(
                key=key,
                agent_id=_as_str(item, "agent_id"),
                username=username,
                name=_as_str(item, "name"),
                system_prompt_path=prompt_path,
                allowed_tools=allowed_tools,
                model=model_config,
            )
        )

    return MeshConfig(defaults=defaults, agents=profiles)
