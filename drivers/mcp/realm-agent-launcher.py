#!/usr/bin/env python3
"""Realm Agent Launcher MCP.

Starts and supervises local Realm workers backed by headless OpenCode.
Realm stays the network layer; this server owns process/config lifecycle.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


# drivers/mcp/ is two levels below repo root; env override always wins.
_DEFAULT_REPO = Path(__file__).resolve().parents[2]
if not (_DEFAULT_REPO / "src" / "agentnet").is_dir():
    # Fallback if layout changes
    _DEFAULT_REPO = Path(__file__).resolve().parents[1]
REALM_REPO = Path(os.getenv("REALM_REPO", _DEFAULT_REPO))
LAUNCHER_HOME = Path(
    os.getenv(
        "REALM_AGENT_LAUNCHER_HOME",
        Path.home() / ".local" / "share" / "realm-agent-launcher",
    )
)
AGENTS_DIR = LAUNCHER_HOME / "agents"
DEFAULT_NATS_URL = os.getenv(
    "REALM_NATS_URL",
    "nats://agentnet_secret_token@localhost:4222",
)
DEFAULT_OPENCODE_BIN = os.getenv("OPENCODE_BIN", "opencode")
DEFAULT_PYTHON = os.getenv(
    "OPENCODE_PYTHON",
    str(REALM_REPO / "venv" / "bin" / "python"),
)
START_SCRIPT = REALM_REPO / "services" / "agent-template" / "start-opencode-agent.sh"
DEFAULT_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"

DEFAULT_REALM_SYSTEM_PROMPT = (
    "You are a Realm network agent backed by headless OpenCode. "
    "Treat the current inbound Realm request as the only authoritative task. "
    "Do not recover or infer work from unrelated dirty files, prior local edits, "
    "or generic workspace state. Ignore unrelated uncommitted changes unless the "
    "request explicitly asks about them or they directly block the requested work. "
    "Use prior context only from the same Realm thread or OpenCode session tied to "
    "that thread."
)


mcp = FastMCP(
    "Realm Agent Launcher",
    instructions=(
        "Launch, list, stop, and restart local Realm agents backed by "
        "headless OpenCode. Use this for agent infrastructure, not for "
        "Realm message routing."
    ),
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8114")),
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def _clean_agent_id(agent_id: str) -> str:
    value = str(agent_id or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,80}", value):
        raise ValueError(
            "agent_id must be 2-80 chars using letters, numbers, dot, dash, or underscore"
        )
    return value


def _agent_home(agent_id: str) -> Path:
    return AGENTS_DIR / _clean_agent_id(agent_id)


def _metadata_path(agent_id: str) -> Path:
    return _agent_home(agent_id) / "launcher.json"


def _quote_env(value: Any) -> str:
    text = str(value)
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _write_env(path: Path, values: dict[str, Any]) -> None:
    lines = [f"{key}={_quote_env(val)}" for key, val in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)


def _running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _compose_model(provider: str | None, model: str | None) -> str:
    model_value = str(model or "").strip()
    provider_value = str(provider or "").strip().strip("/")
    if not model_value:
        return ""
    if "/" in model_value or not provider_value:
        return model_value
    return f"{provider_value}/{model_value}"


def _load_opencode_config(opencode_config: str | None) -> dict[str, Any]:
    source = Path(opencode_config).expanduser() if opencode_config else DEFAULT_CONFIG
    data = _read_json(source, {})
    return data if isinstance(data, dict) else {}


def _ensure_opencode_config(
    *,
    agent_home: Path,
    workspace: str,
    port: int,
    opencode_agent: str,
    opencode_config: str | None,
) -> Path:
    config = _load_opencode_config(opencode_config)
    config.setdefault("$schema", "https://opencode.ai/config.json")
    server = config.setdefault("server", {})
    if isinstance(server, dict):
        server["hostname"] = "127.0.0.1"
        server["port"] = port

    agents = config.setdefault("agent", {})
    if isinstance(agents, dict):
        agent_cfg = agents.setdefault(opencode_agent, {})
        if isinstance(agent_cfg, dict):
            permission = agent_cfg.setdefault("permission", {})
            if isinstance(permission, dict):
                workspace_glob = str(Path(workspace).expanduser().resolve()) + "/**"
                for tool_name in (
                    "read",
                    "edit",
                    "write",
                    "glob",
                    "grep",
                    "bash",
                    "external_directory",
                ):
                    rule = permission.setdefault(tool_name, {})
                    if isinstance(rule, dict):
                        rule.setdefault(workspace_glob, "allow")
                permission.setdefault("skill", "allow")

    config_path = agent_home / "opencode.json"
    _write_json(config_path, config)
    return config_path


def _metadata(agent_id: str) -> dict[str, Any]:
    data = _read_json(_metadata_path(agent_id), {})
    return data if isinstance(data, dict) else {}


def _agent_status(agent_id: str) -> dict[str, Any]:
    meta = _metadata(agent_id)
    pid = int(meta.get("pid") or 0)
    running = _running(pid)
    meta["running"] = running
    meta["status"] = "running" if running else "stopped"
    return meta


def _read_process_stats(pid: int) -> dict[str, Any]:
    if not _running(pid):
        return {"pid": pid, "running": False}

    try:
        proc = subprocess.run(
            [
                "ps",
                "-o",
                "pid=,ppid=,%mem=,rss=,etime=,state=,command=",
                "-p",
                str(pid),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return {"pid": pid, "running": True, "error": f"ps_failed: {exc}"}

    line = proc.stdout.strip()
    if not line:
        return {"pid": pid, "running": False}

    parts = line.split(None, 6)
    if len(parts) < 7:
        return {"pid": pid, "running": True, "raw": line}

    rss_kb = int(parts[3])
    return {
        "pid": int(parts[0]),
        "ppid": int(parts[1]),
        "mem_percent": float(parts[2]),
        "rss_kb": rss_kb,
        "rss_mb": round(rss_kb / 1024, 1),
        "elapsed": parts[4],
        "state": parts[5],
        "command": parts[6],
        "running": True,
    }


def _list_agent_ids() -> list[str]:
    if not AGENTS_DIR.exists():
        return []
    return sorted(p.name for p in AGENTS_DIR.iterdir() if p.is_dir())


@mcp.tool()
def list_launched_agents() -> str:
    """List OpenCode-backed Realm agents launched by this MCP."""
    return _json([_agent_status(agent_id) for agent_id in _list_agent_ids()])


@mcp.tool()
def get_launched_agent_stats(agent_id: str = "") -> str:
    """Show RAM/process stats for one launched agent or all launched agents."""
    agent_ids = [_clean_agent_id(agent_id)] if agent_id else _list_agent_ids()
    items: list[dict[str, Any]] = []
    total_rss_kb = 0

    for current_id in agent_ids:
        status = _agent_status(current_id)
        pid = int(status.get("pid") or 0)
        proc = _read_process_stats(pid) if pid else {"pid": 0, "running": False}
        total_rss_kb += int(proc.get("rss_kb") or 0)
        items.append(
            {
                "agent_id": current_id,
                "status": status.get("status"),
                "workspace": status.get("workspace"),
                "model": status.get("model"),
                "provider": status.get("provider"),
                "port": status.get("port"),
                "process": proc,
            }
        )

    return _json(
        {
            "ok": True,
            "count": len(items),
            "total_rss_kb": total_rss_kb,
            "total_rss_mb": round(total_rss_kb / 1024, 1),
            "agents": items,
        }
    )


@mcp.tool()
def launch_opencode_agent(
    agent_id: str,
    workspace: str,
    model: str = "",
    provider: str = "",
    opencode_agent: str = "build",
    port: int = 0,
    agent_name: str = "",
    username: str = "",
    nats_url: str = "",
    system_prompt: str = "",
    opencode_bin: str = "",
    opencode_config: str = "",
    extra_env: dict[str, str] | None = None,
    tools: list[str] | None = None,
    force: bool = False,
) -> str:
    """Launch a local Realm worker using the OpenCode harness.

    agent_id: stable Realm identity, without @.
    workspace: project directory OpenCode should work in.

    Workspace hygiene (important):
    - Prefer ONE writer agent per workspace. Multiple agents writing the same tree
      is NOT clean (git conflicts, half-applied edits, lock thrash).
    - Safe sharing: several agents with read-only review on one workspace, or
      one writer + one reviewer that does not edit.
    - Parallel writers: give each agent its own git worktree (or clone), e.g.
      ../Realm-worktrees/@coder-a, not the same path for a/b/c/d/e.

    model/provider: either model='provider/model' or provider='deepseek', model='...'.
    opencode_agent: OpenCode primary agent name. Defaults to build; do not use subagents.
    opencode_config: optional OpenCode config JSON to copy/patch for this worker.
    tools: optional note of intended tools; actual tool config comes from opencode_config.
    """
    clean_id = _clean_agent_id(agent_id)
    home = _agent_home(clean_id)
    meta_path = _metadata_path(clean_id)
    existing = _agent_status(clean_id) if meta_path.exists() else {}
    if existing.get("running") and not force:
        return _json(
            {
                "ok": False,
                "error": "agent_already_running",
                "agent": existing,
                "hint": "pass force=true or stop/restart the agent first",
            }
        )

    if existing.get("running") and force:
        stop_launched_agent(clean_id)

    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        raise ValueError(f"workspace does not exist or is not a directory: {workspace_path}")

    home.mkdir(parents=True, exist_ok=True)
    logs_dir = home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    selected_port = int(port or _free_port())
    selected_model = _compose_model(provider, model)
    selected_name = agent_name or clean_id
    selected_username = username or clean_id
    selected_bin = opencode_bin or DEFAULT_OPENCODE_BIN
    config_path = _ensure_opencode_config(
        agent_home=home,
        workspace=str(workspace_path),
        port=selected_port,
        opencode_agent=opencode_agent,
        opencode_config=opencode_config or None,
    )

    env_path = home / ".env"
    env_values: dict[str, Any] = {
        "REALM_AGENT_ID": clean_id,
        "REALM_AGENT_NAME": selected_name,
        "REALM_USERNAME": selected_username,
        "REALM_AGENT_HOME": str(home),
        "REALM_NATS_URL": nats_url or DEFAULT_NATS_URL,
        "REALM_REPO": str(REALM_REPO),
        "REALM_WRAPPER": str(REALM_REPO / "examples" / "opencode_realm_agent.py"),
        "REALM_BLOB_DIR": str(home / ".blobs" / "agent"),
        "REALM_OPENCODE_SESSION_MAP": str(home / ".realm" / f"{clean_id}-sessions.json"),
        "REALM_SYSTEM_PROMPT": system_prompt
        or f"You are {clean_id}. {DEFAULT_REALM_SYSTEM_PROMPT}",
        "OPENCODE_BIN": selected_bin,
        "OPENCODE_PYTHON": DEFAULT_PYTHON,
        "OPENCODE_HOST": "127.0.0.1",
        "OPENCODE_PORT": selected_port,
        "OPENCODE_URL": f"http://127.0.0.1:{selected_port}",
        "OPENCODE_AGENT": opencode_agent,
        "OPENCODE_MODEL": selected_model,
        "OPENCODE_DIR": str(workspace_path),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_LOG": str(logs_dir / "opencode.log"),
        "REALM_LOG": str(logs_dir / "realm.log"),
        "OPENCODE_TIMEOUT": os.getenv("REALM_LAUNCHER_OPENCODE_TIMEOUT", "86400"),
    }
    if extra_env:
        for key, value in extra_env.items():
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(key)):
                env_values[str(key)] = str(value)
    _write_env(env_path, env_values)

    launcher_log = logs_dir / "launcher.log"
    with launcher_log.open("ab") as log:
        proc = subprocess.Popen(
            [str(START_SCRIPT)],
            cwd=str(home),
            env={
                "HOME": str(Path.home()),
                "PATH": os.getenv(
                    "PATH",
                    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                ),
                "REALM_AGENT_ID": clean_id,
                "REALM_AGENT_HOME": str(home),
                "REALM_AGENT_ENV": str(env_path),
            },
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

    meta = {
        "agent_id": clean_id,
        "username": selected_username,
        "agent_name": selected_name,
        "harness": "opencode",
        "provider": provider,
        "model": selected_model,
        "opencode_agent": opencode_agent,
        "workspace": str(workspace_path),
        "pid": proc.pid,
        "port": selected_port,
        "opencode_url": f"http://127.0.0.1:{selected_port}",
        "home": str(home),
        "env_path": str(env_path),
        "opencode_config": str(config_path),
        "logs": {
            "launcher": str(launcher_log),
            "opencode": str(logs_dir / "opencode.log"),
            "realm": str(logs_dir / "realm.log"),
        },
        "tools": tools or [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json(meta_path, meta)
    time.sleep(1)
    return _json({"ok": True, "agent": _agent_status(clean_id)})


@mcp.tool()
def stop_launched_agent(agent_id: str, timeout_seconds: float = 8.0) -> str:
    """Stop a launched local Realm/OpenCode agent by process group."""
    clean_id = _clean_agent_id(agent_id)
    meta = _metadata(clean_id)
    pid = int(meta.get("pid") or 0)
    if not _running(pid):
        meta["status"] = "stopped"
        meta["running"] = False
        if meta:
            _write_json(_metadata_path(clean_id), meta)
        return _json({"ok": True, "agent_id": clean_id, "status": "already_stopped"})

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        os.kill(pid, signal.SIGTERM)

    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if not _running(pid):
            break
        time.sleep(0.25)
    if _running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            os.kill(pid, signal.SIGKILL)

    meta["status"] = "stopped"
    meta["running"] = False
    meta["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_json(_metadata_path(clean_id), meta)
    return _json({"ok": True, "agent": meta})


@mcp.tool()
def restart_opencode_agent(
    agent_id: str,
    model: str = "",
    provider: str = "",
    workspace: str = "",
    opencode_agent: str = "",
) -> str:
    """Restart an agent, optionally changing model/provider/workspace/primary agent."""
    clean_id = _clean_agent_id(agent_id)
    meta = _metadata(clean_id)
    if not meta:
        return _json({"ok": False, "error": "unknown_agent", "agent_id": clean_id})

    stop_launched_agent(clean_id)
    return launch_opencode_agent(
        agent_id=clean_id,
        workspace=workspace or str(meta["workspace"]),
        model=model or str(meta.get("model") or ""),
        provider=provider or str(meta.get("provider") or ""),
        opencode_agent=opencode_agent or str(meta.get("opencode_agent") or "build"),
        port=int(meta.get("port") or 0),
        agent_name=str(meta.get("agent_name") or clean_id),
        username=str(meta.get("username") or clean_id),
        opencode_config=str(meta.get("opencode_config") or ""),
        force=True,
    )


@mcp.tool()
def show_agent_launcher_paths() -> str:
    """Show launcher install paths and the Codex MCP snippet to enable it."""
    return _json(
        {
            "realm_repo": str(REALM_REPO),
            "launcher_mcp": str(Path(__file__).resolve()),
            "launcher_home": str(LAUNCHER_HOME),
            "start_script": str(START_SCRIPT),
            "codex_config": str(Path.home() / ".codex" / "config.toml"),
            "stdio_wrapper": str(Path.home() / ".local" / "bin" / "realm-agent-launcher-stdio"),
            "codex_mcp_server": "realm_agent_launcher",
        }
    )


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
