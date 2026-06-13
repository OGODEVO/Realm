#!/usr/bin/env python3
"""Realm agent backed by a headless OpenCode server.

Keeps a Realm identity online, receives messages from teammates, forwards
each prompt to OpenCode, and replies.  Direct messages from known teammates
are processed through OpenCode as well, enabling peer-to-peer discussion.

Live tool calls and reasoning are polled from ``opencode export`` and
posted to the Realm thread as structured progress messages so coordinators
can see status in real time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

try:
    from agentnet.sdk import AgentSDK
except ModuleNotFoundError:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from agentnet.sdk import AgentSDK

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
NATS_URL      = os.getenv("REALM_NATS_URL",    "nats://agentnet_secret_token@localhost:4222")
AGENT_ID      = os.getenv("REALM_AGENT_ID",     "opencode-agent")
AGENT_NAME    = os.getenv("REALM_AGENT_NAME",   "OpenCode Agent")
USERNAME      = os.getenv("REALM_USERNAME",     "opencode-agent")

OPENCODE_BIN  = os.getenv("OPENCODE_BIN",       "opencode")
OPENCODE_URL  = os.getenv("OPENCODE_URL",       "http://127.0.0.1:4096")
OPENCODE_USER = os.getenv("OPENCODE_SERVER_USERNAME",  "opencode")
OPENCODE_PASS = os.getenv("OPENCODE_SERVER_PASSWORD",  "")
OPENCODE_MODEL= os.getenv("OPENCODE_MODEL",     "")
OPENCODE_AGENT= os.getenv("OPENCODE_AGENT",     "build")
OPENCODE_DIR  = os.getenv("OPENCODE_DIR",       os.getcwd())
OPENCODE_TMO  = float(os.getenv("OPENCODE_TIMEOUT", "180"))

BLOB_DIR      = os.getenv("REALM_BLOB_DIR") or None
SYSTEM_PROMPT = os.getenv(
    "REALM_SYSTEM_PROMPT",
    "You are a Realm agent backed by headless OpenCode.",
)

SESSION_MAP_PATH = Path(
    os.getenv("REALM_OPENCODE_SESSION_MAP", ".realm/opencode_sessions.json"),
)

EXPORT_POLL_S  = float(os.getenv("REALM_EXPORT_POLL_S", "3"))

STATE_DIR_DEFAULT = os.path.join(
    os.path.expanduser("~"), ".local", "share", AGENT_ID, "state",
)
STATE_DIR     = os.getenv("REALM_STATE_DIR", STATE_DIR_DEFAULT)
STATE_PATH    = os.path.join(STATE_DIR, f"{USERNAME}.json")

AGENT_STATE_BIN = os.getenv(
    "AGENT_STATE_SCRIPT",
    os.path.expanduser("~/.local/bin/agent-state-update"),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_session_map() -> dict[str, str]:
    try:
        data = json.loads(SESSION_MAP_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in (data.items() if isinstance(data, dict) else [])}


def save_session_map(session_map: dict[str, str]) -> None:
    SESSION_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_MAP_PATH.write_text(json.dumps(session_map, indent=2, sort_keys=True))


def extract_text_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("text") or "")
    if isinstance(payload, str):
        return payload
    return ""


def extract_text_event(event: dict[str, Any]) -> str:
    etype = str(event.get("type") or "")
    if etype == "text":
        t = event.get("part", {}).get("text")
        return t if isinstance(t, str) else ""
    part = event.get("part")
    if isinstance(part, dict):
        t = part.get("text") or part.get("delta")
        return t if isinstance(t, str) else ""
    t = event.get("delta") or event.get("text")
    return t if isinstance(t, str) else ""


def read_agent_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.loads(fh.read())
    except Exception:
        return {"agent": USERNAME, "state": "idle"}


async def update_agent_state(state: str, **extra: str) -> None:
    """Fire-and-forget, best-effort."""
    args = [AGENT_STATE_BIN, "--agent", USERNAME, "--state", state]
    for key, val in extra.items():
        if val:
            args.extend([f"--{key.replace('_', '-')}", str(val)])
    try:
        proc = await asyncio.create_subprocess_exec(*args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    except Exception:
        pass


# -- cancellation helpers ----------------------------------------------------

_CANCEL_RE = re.compile(r"task_id=(\S+)", re.IGNORECASE)


def _text_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("text") or "")
    if isinstance(payload, str):
        return payload
    return ""


def is_cancel_msg(payload: Any) -> bool:
    return _text_from_payload(payload).strip().upper().startswith("CANCEL")


def extract_cancel_task_id(payload: Any) -> str:
    if isinstance(payload, dict):
        tid = str(payload.get("task_id") or "").strip()
        if tid:
            return tid
    m = _CANCEL_RE.search(_text_from_payload(payload))
    return m.group(1) if m else ""


# -- teammate detection ------------------------------------------------------

_TEAM_USERS = frozenset({
    "eng-m2", "m4-dl", "medusa-bridge", "m2-opencode-mcp",
    "acct_01kte0xax29t2nkx5smpkt5aw4",   # eng-m2
    "acct_01ktdcv4wsjrpdzp0mmpxj6kj2",   # m2-opencode-mcp
    "acct_01ktczjxzaj2zf0pj5z45h1pzj",   # medusa-bridge
})


def is_from_teammate(msg: Any) -> bool:
    return (str(msg.from_account_id or "") in _TEAM_USERS or
            str(msg.from_agent or "") in _TEAM_USERS)


# -- status messaging --------------------------------------------------------

def _progress_json(subtype: str, text: str, task_id: str = "",
                   visible: bool = False) -> str:
    return json.dumps({
        "type": "progress", "subtype": subtype, "text": text,
        "task_id": task_id, "visible_by_default": visible,
    })


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

async def poll_and_stream(session_id: str, sdk: AgentSDK,
                          to_agent: str, thread_id: str,
                          done: asyncio.Event) -> None:
    """Export-poll for new text/reasoning and post structured progress."""
    seen: set[str] = set()
    while not done.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                OPENCODE_BIN, "export", session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                await asyncio.sleep(EXPORT_POLL_S)
                continue
            lines = stdout.decode("utf-8", errors="replace").splitlines()
            json_start = next((i for i, l in enumerate(lines)
                               if l.strip().startswith("{")), -1)
            if json_start < 0:
                await asyncio.sleep(EXPORT_POLL_S)
                continue
            data = json.loads("\n".join(lines[json_start:]))
            for msg in (data.get("messages") or []):
                if (msg.get("info") or {}).get("role") != "assistant":
                    continue
                for part in (msg.get("parts") or []):
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    text  = part.get("text", "")
                    if not isinstance(text, str) or not text:
                        continue
                    key = f"{ptype}:{hash(text)}"
                    if key in seen:
                        continue
                    seen.add(key)
                    is_visible = ptype != "reasoning"
                    subtype = {"reasoning": "thinking",
                               "text": "text"}.get(ptype, ptype)
                    try:
                        await sdk.send_text(to_agent,
                            _progress_json(subtype, text,
                                           visible=is_visible),
                            thread_id=thread_id)
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(EXPORT_POLL_S)


def _export_text(session_id: str) -> str:
    """Return the latest assistant text from an export, or ''."""
    async def _run() -> str:
        proc = await asyncio.create_subprocess_exec(
            OPENCODE_BIN, "export", session_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        json_start = next((i for i, l in enumerate(lines)
                           if l.strip().startswith("{")), -1)
        if json_start < 0:
            return ""
        try:
            data = json.loads("\n".join(lines[json_start:]))
        except json.JSONDecodeError:
            return ""
        # ── reproduce existing extract_exported_text logic ──────────────
        latest: list[str] = []
        for msg in (data.get("messages") or []):
            info = msg.get("info") or {}
            if info.get("role") != "assistant":
                continue
            cur: list[str] = []
            for part in (msg.get("parts") or []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    t = part.get("text")
                    if isinstance(t, str):
                        cur.append(t)
            if cur:
                latest = cur
        return "".join(latest).strip()

    return _run()


async def ask_opencode(prompt: str, *,
                       realm_thread_id: str | None,
                       session_map: dict[str, str],
                       sdk: AgentSDK | None = None,
                       to_agent: str = "",
                       thread_id: str = "") -> str:
    cmd = [
        OPENCODE_BIN, "run", "--attach", OPENCODE_URL,
        "--username", OPENCODE_USER,
        "--dir", OPENCODE_DIR,
        "--agent", OPENCODE_AGENT,
        "--format", "json",
    ]
    if OPENCODE_PASS:
        cmd.extend(["--password", OPENCODE_PASS])
    if OPENCODE_MODEL:
        cmd.extend(["--model", OPENCODE_MODEL])
    map_key = realm_thread_id or ""
    if existing := session_map.get(map_key):
        cmd.extend(["--session", existing])
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    session_id: str | None = None
    try:
        first = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        session_id = json.loads(first.decode()).get("sessionID")
    except Exception:
        pass

    done = asyncio.Event()
    poll_task: asyncio.Task | None = None
    if session_id and sdk and to_agent and thread_id:
        poll_task = asyncio.create_task(
            poll_and_stream(session_id, sdk, to_agent, thread_id, done))

    try:
        stdout, stderr = await proc.communicate()
    finally:
        done.set()
        if poll_task:
            try: poll_task.cancel()
            except Exception: pass

    if proc.returncode != 0:
        raise RuntimeError(
            f"OpenCode exited {proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}")

    chunks: list[str] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not (line := line.strip()):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t := extract_text_event(event):
            chunks.append(t)

    if map_key and session_id:
        session_map[map_key] = session_id
        save_session_map(session_map)

    if answer := "".join(chunks).strip():
        return answer

    # ── fallback: poll export forever ─────────────────────────────────────
    if session_id:
        await asyncio.sleep(3)
        while True:
            if (exported := await _export_text(session_id)):
                return exported
            if read_agent_state().get("state") in ("cancelling", "cancelled"):
                return "CANCELLED"
            await asyncio.sleep(EXPORT_POLL_S)

    return "(OpenCode returned no text.)"


# ---------------------------------------------------------------------------
# Message handler – called by AgentSDK.receive
# ---------------------------------------------------------------------------

def _build_reply(text: str, status: str, task_id: str = "") -> dict[str, Any]:
    return {"text": text, "agent": USERNAME,
            "status": status, "model": OPENCODE_MODEL, "task_id": task_id}


async def handle_message(sdk: AgentSDK, session_map: dict[str, str],
                         msg: Any) -> None:
    """Single entry-point for every Realm message the agent receives."""
    text = extract_text_payload(msg.payload)
    to   = msg.from_account_id or msg.from_agent or ""
    task_id = ""
    if isinstance(msg.payload, dict):
        task_id = str(msg.payload.get("task_id") or "")
    should_process = (msg.kind == "request" or
                      (msg.kind == "direct" and is_from_teammate(msg)))

    # -- CANCEL (any kind) ---------------------------------------------------
    if is_cancel_msg(msg.payload):
        cancel_id = extract_cancel_task_id(msg.payload)
        current = read_agent_state()
        if cancel_id and cancel_id == str(current.get("task_id") or ""):
            await update_agent_state("cancelled", task_id=cancel_id,
                                     error="cancelled by coordinator")
            await sdk.send_text(to,
                json.dumps({"type": "status", "subtype": "cancelled",
                            "task_id": cancel_id, "text": "Task cancelled"}),
                thread_id=msg.thread_id)
        return

    # -- STATE query (any kind) ----------------------------------------------
    if text.strip().upper() == "STATE":
        state_data = read_agent_state()
        state_json = json.dumps({"type": "state", "data": state_data})
        if msg.kind == "request":
            await sdk.node.reply(msg, _build_reply(state_json, "answered"),
                                 thread_id=msg.thread_id)
        else:
            await sdk.send_text(to, state_json, thread_id=msg.thread_id)
        return

    # -- ignore noise --------------------------------------------------------
    if not should_process:
        print(f"received {msg.kind} from {to}: {text}", flush=True)
        return

    task_id = task_id or msg.trace_id or ""

    # -- pre-check cancellation ----------------------------------------------
    if read_agent_state().get("state") == "cancelling":
        await update_agent_state("cancelled", task_id=task_id,
                                 error="cancelled before start")
        await sdk.send_text(to,
            json.dumps({"type": "status", "subtype": "cancelled",
                        "task_id": task_id, "text": "Task cancelled"}),
            thread_id=msg.thread_id)
        return

    # -- ACK ----------------------------------------------------------------
    await update_agent_state("acknowledged", task_id=task_id,
                             thread_id=msg.thread_id,
                             last_action=text.strip()[:200])
    try:
        await sdk.send_text(to,
            _progress_json("status", f"ACK: {text.strip()[:150]}",
                           task_id, visible=True),
            thread_id=msg.thread_id)
    except Exception:
        pass

    # -- WORKING -------------------------------------------------------------
    await update_agent_state("working", task_id=task_id,
                             thread_id=msg.thread_id)
    try:
        await sdk.send_text(to,
            _progress_json("status", f"WORKING: {text.strip()[:150]}",
                           task_id, visible=True),
            thread_id=msg.thread_id)
    except Exception:
        pass

    # -- execute -------------------------------------------------------------
    try:
        answer = await ask_opencode(
            f"{SYSTEM_PROMPT}\n\nRequest:\n{text}",
            realm_thread_id=msg.thread_id,
            session_map=session_map,
            sdk=sdk, to_agent=to, thread_id=msg.thread_id)
        await update_agent_state("done", task_id=task_id,
                                 last_action=answer[:200])
        payload = _build_reply(answer, "answered", task_id)
    except Exception as exc:
        await update_agent_state("failed", task_id=task_id,
                                 error=str(exc)[:500])
        payload = _build_reply(f"OpenCode handler failed: {exc}", "error",
                               task_id)

    if msg.kind == "request":
        await sdk.node.reply(msg, payload, thread_id=msg.thread_id)
    elif to:
        await sdk.send_text(to, json.dumps(payload), thread_id=msg.thread_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    session_map = load_session_map()

    sdk = AgentSDK(
        agent_id   = AGENT_ID,
        name       = AGENT_NAME,
        username   = USERNAME,
        capabilities = ["llm", "coding-agent", "opencode-headless"],
        nats_url   = NATS_URL,
        blob_store_dir = BLOB_DIR,
        heartbeat_interval = 12.0,
        work_timeout_seconds = max(OPENCODE_TMO + 30.0, 60.0),
        metadata   = {
            "kind": "opencode-llm-agent",
            "opencode_url": OPENCODE_URL,
            "opencode_dir": OPENCODE_DIR,
            "opencode_model": OPENCODE_MODEL,
        },
    )

    sdk.receive(lambda msg: handle_message(sdk, session_map, msg))

    await sdk.start()
    print(f"{USERNAME} online as {sdk.account_id}", flush=True)
    try:
        await stop.wait()
    finally:
        await sdk.stop()


if __name__ == "__main__":
    asyncio.run(main())
