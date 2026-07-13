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
    from agentnet.task_protocol import (
        TASK_ASSIGN,
        build_task_progress,
        build_task_result,
        task_type,
    )
except ModuleNotFoundError:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from agentnet.sdk import AgentSDK
    from agentnet.task_protocol import (
        TASK_ASSIGN,
        build_task_progress,
        build_task_result,
        task_type,
    )

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
OPENCODE_TMO  = float(os.getenv("OPENCODE_TIMEOUT", "1800"))
# Node-level handler budget must outlive OpenCode. Short budgets cancel long jobs mid-flight.
WORK_TIMEOUT  = float(os.getenv("REALM_WORK_TIMEOUT_SECONDS", "86400"))

BLOB_DIR      = os.getenv("REALM_BLOB_DIR") or None
SYSTEM_PROMPT = os.getenv(
    "REALM_SYSTEM_PROMPT",
    "You are a Realm agent backed by headless OpenCode.",
)
EXECUTION_CONTRACT = (
    "Execution contract: Keep working autonomously until the task is complete "
    "or genuinely blocked on one specific piece of user input. Every terminal "
    "answer MUST end with exactly one status marker: [REALM_TASK_COMPLETE] when "
    "the requested work is finished, or [REALM_TASK_BLOCKED] when user input is "
    "required. Do not emit either marker while work remains."
)

SESSION_MAP_PATH = Path(
    os.getenv("REALM_OPENCODE_SESSION_MAP", ".realm/opencode_sessions.json"),
)

EXPORT_POLL_S  = float(os.getenv("REALM_EXPORT_POLL_S", "3"))
EXPORT_TIMEOUT = float(os.getenv("REALM_EXPORT_TIMEOUT", "20"))
EXPORT_FALLBACK_TIMEOUT = float(
    os.getenv("REALM_EXPORT_FALLBACK_TIMEOUT", "30")
)
MAX_AGENT_TURNS = max(1, int(os.getenv("REALM_MAX_AGENT_TURNS", "56")))
TASK_COMPLETE_MARKER = "[REALM_TASK_COMPLETE]"
TASK_BLOCKED_MARKER = "[REALM_TASK_BLOCKED]"

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
    """Legacy chat-shaped progress (non-task messages only)."""
    return json.dumps({
        "type": "progress", "subtype": subtype, "text": text,
        "task_id": task_id, "visible_by_default": visible,
    })


def _clip(text: str, limit: int = 400) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


async def emit_progress(
    sdk: AgentSDK | None,
    to_agent: str,
    *,
    thread_id: str,
    task_id: str,
    text: str,
    phase: str = "working",
    percent: float | int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit registry-visible task.progress when task_id is known; else chat progress."""
    if not sdk or not to_agent:
        return
    body = _clip(text, 500)
    if not body:
        return
    try:
        if task_id:
            await sdk.report_progress(
                to_agent,
                task_id,
                body,
                thread_id=thread_id or None,
                percent=percent,
                phase=phase,
                metadata=metadata,
                require_delivery_ack=False,
            )
        else:
            await sdk.send_text(
                to_agent,
                _progress_json(phase, body, visible=True),
                thread_id=thread_id or None,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

async def stop_subprocess(proc: asyncio.subprocess.Process) -> None:
    """Stop a child process without leaving a zombie behind."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def communicate_with_timeout(
    proc: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    """Communicate with a child and always reap it on timeout or cancellation."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await stop_subprocess(proc)
        raise
    except asyncio.CancelledError:
        await stop_subprocess(proc)
        raise


def _part_progress_event(part: dict[str, Any]) -> tuple[str, str] | None:
    """Map an OpenCode export part to (phase, text) for network progress."""
    ptype = str(part.get("type") or "").strip().lower()
    if ptype in {"reasoning", "thinking"}:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return "thinking", _clip(text, 280)
        return None
    if ptype in {"text", "message"}:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            cleaned = clean_task_answer(text)
            if cleaned:
                return "text", _clip(cleaned, 400)
        return None
    if ptype in {"tool", "tool_call", "tool-call", "function", "function_call"}:
        fn = part.get("function") if isinstance(part.get("function"), dict) else {}
        name = (
            part.get("name")
            or part.get("tool")
            or part.get("toolName")
            or fn.get("name")
            or "tool"
        )
        status = part.get("status") or part.get("state") or ""
        detail = part.get("input") or part.get("arguments") or part.get("args") or part.get("text") or ""
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, default=str)
        detail_s = _clip(str(detail), 180)
        label = str(name)
        if status:
            label = f"{name} ({status})"
        if detail_s:
            label = f"{label}: {detail_s}"
        return "tool", label
    # Generic fallback for unknown structured parts with a name.
    name = part.get("name") or part.get("tool")
    if name:
        return "tool", _clip(str(name), 200)
    text = part.get("text")
    if isinstance(text, str) and text.strip() and ptype:
        return ptype, _clip(text, 300)
    return None


async def poll_and_stream(
    session_id: str,
    sdk: AgentSDK,
    to_agent: str,
    thread_id: str,
    done: asyncio.Event,
    *,
    task_id: str = "",
) -> None:
    """Export-poll for tool/text activity and post registry-visible progress."""
    seen: set[str] = set()
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_emit = started
    heartbeat_s = float(os.getenv("REALM_PROGRESS_HEARTBEAT_S", "30"))
    while not done.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                OPENCODE_BIN, "export", session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                stdout, _ = await communicate_with_timeout(
                    proc, EXPORT_TIMEOUT
                )
            except TimeoutError:
                await asyncio.sleep(EXPORT_POLL_S)
                continue
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
            messages = data.get("messages") or []
            last_user = max(
                (i for i, msg in enumerate(messages)
                 if (msg.get("info") or {}).get("role") == "user"),
                default=-1,
            )
            for msg in messages[last_user + 1:]:
                if (msg.get("info") or {}).get("role") != "assistant":
                    continue
                for part in (msg.get("parts") or []):
                    if not isinstance(part, dict):
                        continue
                    event = _part_progress_event(part)
                    if event is None:
                        continue
                    phase, text = event
                    key = f"{phase}:{hash(text)}"
                    if key in seen:
                        continue
                    seen.add(key)
                    # Skip pure thinking noise unless no task (chat) — still useful lightly.
                    if phase == "thinking" and task_id:
                        continue
                    last_emit = loop.time()
                    await emit_progress(
                        sdk,
                        to_agent,
                        thread_id=thread_id,
                        task_id=task_id,
                        text=text,
                        phase=phase,
                        metadata={"source": "opencode-export", "part_type": phase},
                    )
            # Heartbeat when tools go silent for too long.
            if task_id and heartbeat_s > 0 and (loop.time() - last_emit) >= heartbeat_s:
                elapsed = int(loop.time() - started)
                last_emit = loop.time()
                await emit_progress(
                    sdk,
                    to_agent,
                    thread_id=thread_id,
                    task_id=task_id,
                    text=f"HEARTBEAT: still running (opencode, {elapsed}s elapsed)",
                    phase="status",
                    metadata={"source": "opencode-export", "heartbeat": True, "elapsed_s": elapsed},
                )
        except Exception:
            pass
        await asyncio.sleep(EXPORT_POLL_S)


def extract_current_turn_text(data: dict[str, Any]) -> str:
    """Return assistant text produced after the export's latest user message."""
    messages = data.get("messages") or []
    last_user = max(
        (i for i, msg in enumerate(messages)
         if (msg.get("info") or {}).get("role") == "user"),
        default=-1,
    )
    chunks: list[str] = []
    for msg in messages[last_user + 1:]:
        if (msg.get("info") or {}).get("role") != "assistant":
            continue
        for part in msg.get("parts") or []:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n\n".join(chunks).strip()


async def export_text(session_id: str) -> str:
    """Return current-turn assistant text from an export, or an empty string."""
    proc = await asyncio.create_subprocess_exec(
        OPENCODE_BIN, "export", session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await communicate_with_timeout(proc, EXPORT_TIMEOUT)
    except TimeoutError:
        return ""
    if proc.returncode != 0:
        return ""
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    json_start = next((i for i, line in enumerate(lines)
                       if line.strip().startswith("{")), -1)
    if json_start < 0:
        return ""
    try:
        data = json.loads("\n".join(lines[json_start:]))
    except json.JSONDecodeError:
        return ""
    return extract_current_turn_text(data)


async def ask_opencode(prompt: str, *,
                       realm_thread_id: str | None,
                       session_map: dict[str, str],
                       sdk: AgentSDK | None = None,
                       to_agent: str = "",
                       thread_id: str = "",
                       task_id: str = "") -> str:
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
    first = b""
    try:
        first = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        session_id = json.loads(first.decode()).get("sessionID")
    except Exception:
        pass

    if map_key and session_id:
        session_map[map_key] = session_id
        save_session_map(session_map)

    done = asyncio.Event()
    poll_task: asyncio.Task | None = None
    if session_id and sdk and to_agent and thread_id:
        poll_task = asyncio.create_task(
            poll_and_stream(
                session_id,
                sdk,
                to_agent,
                thread_id,
                done,
                task_id=task_id,
            )
        )

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=OPENCODE_TMO
            )
        except TimeoutError as exc:
            await stop_subprocess(proc)
            raise RuntimeError(
                f"OpenCode timed out after {OPENCODE_TMO:g} seconds"
            ) from exc
    finally:
        done.set()
        if poll_task:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)

    if proc.returncode != 0:
        raise RuntimeError(
            f"OpenCode exited {proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}")

    chunks: list[str] = []
    raw_stdout = first + stdout
    for line in raw_stdout.decode("utf-8", errors="replace").splitlines():
        if not (line := line.strip()):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t := extract_text_event(event):
            chunks.append(t)

    if answer := "".join(chunks).strip():
        return answer

    # A completed run can occasionally omit JSON events. Recover from its
    # export, but never leave the Realm request hanging forever.
    if session_id:
        deadline = asyncio.get_running_loop().time() + EXPORT_FALLBACK_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if (exported := await export_text(session_id)):
                return exported
            if read_agent_state().get("state") in ("cancelling", "cancelled"):
                return "CANCELLED"
            await asyncio.sleep(EXPORT_POLL_S)
        raise RuntimeError(
            "OpenCode completed without returning a final text response"
        )

    return "(OpenCode returned no text.)"


def is_recoverable_session_error(exc: Exception) -> bool:
    """Return whether retrying with a fresh backend session is safe enough."""
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    return (
        message.startswith("OpenCode exited ")
        or message == "OpenCode completed without returning a final text response"
        or "Session not found" in message
    )


def task_answer_status(answer: str) -> str | None:
    """Read the explicit agent-to-harness completion protocol marker."""
    has_complete = TASK_COMPLETE_MARKER in answer
    has_blocked = TASK_BLOCKED_MARKER in answer
    if has_complete == has_blocked:
        return None
    return "complete" if has_complete else "blocked"


def clean_task_answer(answer: str) -> str:
    """Remove harness control markers from user-facing text."""
    return (
        str(answer or "")
        .replace(TASK_COMPLETE_MARKER, "")
        .replace(TASK_BLOCKED_MARKER, "")
        .strip()
    )


def task_session_key(thread_id: str | None, task_id: str) -> str:
    """Keep task runs isolated from ad-hoc chat sessions on the same thread."""
    return f"{thread_id or 'no-thread'}::task::{task_id}"


async def ask_opencode_resilient(
    prompt: str,
    *,
    realm_thread_id: str | None,
    session_map: dict[str, str],
    sdk: AgentSDK | None = None,
    to_agent: str = "",
    thread_id: str = "",
    task_id: str = "",
) -> str:
    """Retry once with a fresh OpenCode session while preserving the Realm thread."""
    map_key = realm_thread_id or ""
    existing_session = session_map.get(map_key)
    try:
        return await ask_opencode(
            prompt,
            realm_thread_id=realm_thread_id,
            session_map=session_map,
            sdk=sdk,
            to_agent=to_agent,
            thread_id=thread_id,
            task_id=task_id,
        )
    except Exception as exc:
        if not map_key or not is_recoverable_session_error(exc):
            raise

    # Keep the public Realm thread, but detach its broken OpenCode conversation.
    if existing_session:
        session_map.pop(map_key, None)
        save_session_map(session_map)
    await emit_progress(
        sdk,
        to_agent,
        thread_id=thread_id,
        task_id=task_id,
        text="Backend session recovered; retrying on the same thread.",
        phase="status",
    )
    return await ask_opencode(
        prompt,
        realm_thread_id=realm_thread_id,
        session_map=session_map,
        sdk=sdk,
        to_agent=to_agent,
        thread_id=thread_id,
        task_id=task_id,
    )


async def ask_opencode_until_complete(
    prompt: str,
    *,
    realm_thread_id: str | None,
    session_map: dict[str, str],
    sdk: AgentSDK | None = None,
    to_agent: str = "",
    thread_id: str = "",
    task_id: str = "",
) -> str:
    """Drive the agent until it explicitly reports completion or blockage."""
    next_prompt = prompt
    for _ in range(MAX_AGENT_TURNS):
        answer = await ask_opencode_resilient(
            next_prompt,
            realm_thread_id=realm_thread_id,
            session_map=session_map,
            sdk=sdk,
            to_agent=to_agent,
            thread_id=thread_id,
            task_id=task_id,
        )
        if task_answer_status(answer) is not None:
            return clean_task_answer(answer)
        next_prompt = (
            "The task is still active because your previous turn had no Realm "
            "completion status. Continue executing autonomously. When finished, "
            f"end with {TASK_COMPLETE_MARKER}; if one specific user input blocks "
            f"you, ask for it and end with {TASK_BLOCKED_MARKER}."
        )
    raise RuntimeError(
        f"Agent exceeded {MAX_AGENT_TURNS} turns without reporting complete or blocked"
    )


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
    incoming_task_type = task_type(msg.payload)
    is_task_assignment = incoming_task_type == TASK_ASSIGN
    should_process = (msg.kind == "request" or is_task_assignment or
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
    task_session = task_session_key(msg.thread_id, task_id) if is_task_assignment else msg.thread_id
    task_title = ""
    task_metadata: dict[str, Any] = {}
    if isinstance(msg.payload, dict):
        task_title = str(msg.payload.get("title") or "")
        raw_metadata = msg.payload.get("metadata")
        if isinstance(raw_metadata, dict):
            task_metadata = raw_metadata

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
    await emit_progress(
        sdk,
        to,
        thread_id=msg.thread_id or "",
        task_id=task_id,
        text=f"ACK: accepted task — {_clip(text, 150)}",
        phase="ack",
        percent=1,
    )

    # -- WORKING -------------------------------------------------------------
    await update_agent_state("working", task_id=task_id,
                             thread_id=msg.thread_id)
    await emit_progress(
        sdk,
        to,
        thread_id=msg.thread_id or "",
        task_id=task_id,
        text=f"WORKING: {_clip(text, 150)}",
        phase="working",
        percent=5,
    )

    # -- execute -------------------------------------------------------------
    try:
        task_context = ""
        if is_task_assignment:
            parent_task = ""
            if isinstance(msg.payload, dict):
                parent_task = str(msg.payload.get("parent_task_id") or "")
            task_context = (
                "\n\nRealm task context:\n"
                f"- task_id: {task_id}\n"
                f"- parent_task_id: {parent_task or '(none)'}\n"
                f"- title: {task_title or '(none)'}\n"
                f"- thread_id: {msg.thread_id or '(none)'}\n"
                f"- metadata: {json.dumps(task_metadata, sort_keys=True)}\n"
            )
        answer = await ask_opencode_until_complete(
            f"{SYSTEM_PROMPT}\n\n{EXECUTION_CONTRACT}{task_context}\n\nRequest:\n{text}",
            realm_thread_id=task_session,
            session_map=session_map,
            sdk=sdk, to_agent=to, thread_id=msg.thread_id or "",
            task_id=task_id,
        )
        await update_agent_state("done", task_id=task_id,
                                 last_action=answer[:200])
        payload = (
            build_task_result(
                task_id=task_id,
                text=answer,
                status="completed",
                metadata={"agent": USERNAME, "model": OPENCODE_MODEL},
            )
            if is_task_assignment
            else _build_reply(answer, "answered", task_id)
        )
    except Exception as exc:
        await update_agent_state("failed", task_id=task_id,
                                 error=str(exc)[:500])
        payload = (
            build_task_result(
                task_id=task_id,
                text=f"OpenCode handler failed: {exc}",
                status="failed",
                metadata={"agent": USERNAME, "model": OPENCODE_MODEL},
            )
            if is_task_assignment
            else _build_reply(f"OpenCode handler failed: {exc}", "error",
                              task_id)
        )

    if msg.kind == "request":
        await sdk.node.reply(msg, payload, thread_id=msg.thread_id)
    elif to:
        if is_task_assignment:
            await sdk.send_json(
                to,
                payload,
                thread_id=msg.thread_id,
                idempotency_key=f"{task_id}:result",
                require_delivery_ack=False,
            )
        else:
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
        # Must exceed longest job. Default 24h — short values kill long agentic work.
        work_timeout_seconds = max(WORK_TIMEOUT, OPENCODE_TMO + 60.0, 60.0),
        metadata   = {
            "kind": "opencode-llm-agent",
            "opencode_url": OPENCODE_URL,
            "opencode_dir": OPENCODE_DIR,
            "opencode_model": OPENCODE_MODEL,
            "emits_task_progress": True,
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
