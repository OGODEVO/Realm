#!/usr/bin/env python3
"""Realm agent backed by a headless OpenCode server.

This keeps a Realm identity online, receives request messages, forwards each
prompt to OpenCode, and replies on the original request reply subject.

It also maps Realm thread IDs to OpenCode session IDs so follow-up requests in
the same Realm thread continue the same OpenCode conversation.

While a task is running, live tool calls and reasoning are polled from
`opencode export <sessionID>` and posted to the Realm thread so coordinators
can see progress in real time.
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


NATS_URL = os.getenv("REALM_NATS_URL", "nats://agentnet_secret_token@localhost:4222")
AGENT_ID = os.getenv("REALM_AGENT_ID", "opencode-agent")
AGENT_NAME = os.getenv("REALM_AGENT_NAME", "OpenCode Agent")
USERNAME = os.getenv("REALM_USERNAME", "opencode-agent")

OPENCODE_BIN = os.getenv("OPENCODE_BIN", "opencode")
OPENCODE_URL = os.getenv("OPENCODE_URL", "http://127.0.0.1:4096")
OPENCODE_USERNAME = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
OPENCODE_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "")
OPENCODE_AGENT = os.getenv("OPENCODE_AGENT", "build")
OPENCODE_DIR = os.getenv("OPENCODE_DIR", os.getcwd())
OPENCODE_TIMEOUT = float(os.getenv("OPENCODE_TIMEOUT", "180"))
BLOB_DIR = os.getenv("REALM_BLOB_DIR") or None
SYSTEM_PROMPT = os.getenv(
    "REALM_SYSTEM_PROMPT",
    "You are a Realm agent backed by headless OpenCode. "
    "Answer the request directly and concisely. "
    "Use your configured tools when useful.",
)
SESSION_MAP_PATH = Path(
    os.getenv("REALM_OPENCODE_SESSION_MAP", ".realm/opencode_sessions.json")
)
EXPORT_POLL_INTERVAL = float(os.getenv("REALM_EXPORT_POLL_S", "3"))


def load_session_map() -> dict[str, str]:
    try:
        data = json.loads(SESSION_MAP_PATH.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


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
    event_type = str(event.get("type") or "")

    if event_type == "text":
        text = event.get("part", {}).get("text")
        return text if isinstance(text, str) else ""

    part = event.get("part")
    if isinstance(part, dict):
        text = part.get("text")
        if isinstance(text, str):
            return text

        delta = part.get("delta")
        if isinstance(delta, str):
            return delta

    delta = event.get("delta")
    if isinstance(delta, str):
        return delta

    text = event.get("text")
    if isinstance(text, str):
        return text

    return ""


def extract_exported_text(export_data: dict[str, Any]) -> str:
    messages = export_data.get("messages")
    if not isinstance(messages, list):
        return ""

    latest_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        message_parts = message.get("parts")
        if not isinstance(message_parts, list):
            continue
        current_parts: list[str] = []
        for part in message_parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    current_parts.append(text)
        if current_parts:
            latest_parts = current_parts

    return "".join(latest_parts).strip()


AGENT_STATE_SCRIPT = os.getenv(
    "AGENT_STATE_SCRIPT",
    os.path.expanduser("~/.local/bin/agent-state-update"),
)


async def update_agent_state(state: str, **extra: str) -> None:
    """Call the external state updater (fire-and-forget, best-effort)."""
    args = [
        AGENT_STATE_SCRIPT,
        "--agent", USERNAME,
        "--state", state,
    ]
    for key, val in extra.items():
        if val:
            args.extend([f"--{key.replace('_', '-')}", str(val)])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass  # state update is non-critical


async def poll_and_stream(
    session_id: str,
    sdk: AgentSDK,
    to_agent: str,
    thread_id: str,
    done: asyncio.Event,
) -> None:
    """Poll opencode export every few seconds and post new text/reasoning to the
    Realm thread so coordinators can see live progress."""
    seen: set[str] = set()

    while not done.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                OPENCODE_BIN,
                "export",
                session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            if proc.returncode != 0:
                await asyncio.sleep(EXPORT_POLL_INTERVAL)
                continue

            lines = stdout.decode("utf-8", errors="replace").splitlines()
            json_start = next(
                (i for i, line in enumerate(lines) if line.strip().startswith("{")),
                -1,
            )
            if json_start < 0:
                await asyncio.sleep(EXPORT_POLL_INTERVAL)
                continue

            data = json.loads("\n".join(lines[json_start:]))
            messages = data.get("messages") or []

            for msg in messages:
                info = msg.get("info") or {}
                if info.get("role") != "assistant":
                    continue
                for part in msg.get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    text = part.get("text", "")
                    if not isinstance(text, str) or not text:
                        continue

                    dedup_key = f"{ptype}:{hash(text)}"
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    visible = ptype != "reasoning"
                    subtype = {"reasoning": "thinking", "text": "text"}.get(ptype, ptype)
                    payload = json.dumps({
                        "type": "progress",
                        "subtype": subtype,
                        "text": text,
                        "task_id": "",
                        "visible_by_default": visible,
                    })
                    try:
                        await sdk.send_text(
                            to_agent, payload, thread_id=thread_id
                        )
                    except Exception:
                        pass  # best-effort streaming

        except Exception:
            pass  # best-effort — polling is non-critical

        await asyncio.sleep(EXPORT_POLL_INTERVAL)


async def ask_opencode(
    prompt: str,
    *,
    realm_thread_id: str | None,
    session_map: dict[str, str],
    sdk: AgentSDK | None = None,
    to_agent: str = "",
    thread_id: str = "",
) -> str:
    cmd = [
        OPENCODE_BIN,
        "run",
        "--attach",
        OPENCODE_URL,
        "--username",
        OPENCODE_USERNAME,
        "--dir",
        OPENCODE_DIR,
        "--agent",
        OPENCODE_AGENT,
        "--format",
        "json",
    ]

    if OPENCODE_PASSWORD:
        cmd.extend(["--password", OPENCODE_PASSWORD])
    if OPENCODE_MODEL:
        cmd.extend(["--model", OPENCODE_MODEL])

    map_key = realm_thread_id or ""
    existing_session = session_map.get(map_key)
    if existing_session:
        cmd.extend(["--session", existing_session])

    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # --- read sessionID from the first stdout line ----------------------------
    session_id: str | None = None
    try:
        first_line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        first_event = json.loads(first_line.decode("utf-8", errors="replace"))
        session_id = first_event.get("sessionID")
    except Exception:
        pass

    # --- start export-polling background task --------------------------------
    done = asyncio.Event()
    poll_task: asyncio.Task | None = None
    if session_id and sdk is not None and to_agent and thread_id:
        poll_task = asyncio.create_task(
            poll_and_stream(session_id, sdk, to_agent, thread_id, done)
        )

    # --- wait for opencode to accept the prompt (exits almost immediately) ------
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        done.set()
        if poll_task is not None:
            poll_task.cancel()
        raise TimeoutError(f"OpenCode timed out after {OPENCODE_TIMEOUT:.0f}s")
    finally:
        done.set()
        if poll_task is not None:
            try:
                poll_task.cancel()
            except Exception:
                pass

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"OpenCode exited {proc.returncode}: {stderr_text}")

    # --- parse JSON output for text chunks -----------------------------------
    chunks: list[str] = []
    first_session_id: str | None = session_id

    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        sid = event.get("sessionID")
        if isinstance(sid, str) and sid and first_session_id is None:
            first_session_id = sid

        text = extract_text_event(event)
        if text:
            chunks.append(text)

    if map_key and first_session_id:
        session_map[map_key] = first_session_id
        save_session_map(session_map)

    answer = "".join(chunks).strip()
    if answer:
        return answer

    # --- poll export until text appears (no timeout — wait for completion) -----
    if first_session_id:
        await asyncio.sleep(3)
        while True:
            export_proc = await asyncio.create_subprocess_exec(
                OPENCODE_BIN, "export", first_session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            export_stdout, _export_stderr = await export_proc.communicate()
            if export_proc.returncode == 0:
                lines = export_stdout.decode("utf-8", errors="replace").splitlines()
                json_start = next(
                    (i for i, line in enumerate(lines) if line.strip().startswith("{")),
                    -1,
                )
                if json_start >= 0:
                    try:
                        export_data = json.loads("\n".join(lines[json_start:]))
                    except json.JSONDecodeError:
                        export_data = None
                    if isinstance(export_data, dict):
                        exported_answer = extract_exported_text(export_data)
                        if exported_answer:
                            return exported_answer

            # -- cancellation check during polling --------------------------
            try:
                state_data = _read_state()
                if state_data.get("state") in ("cancelling", "cancelled"):
                    return "CANCELLED"
            except Exception:
                pass

            await asyncio.sleep(EXPORT_POLL_INTERVAL)

    if stderr_text:
        return stderr_text
    return "(OpenCode returned no text.)"


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    session_map = load_session_map()

    sdk = AgentSDK(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        username=USERNAME,
        capabilities=["llm", "coding-agent", "opencode-headless"],
        nats_url=NATS_URL,
        blob_store_dir=BLOB_DIR,
        heartbeat_interval=12.0,
        work_timeout_seconds=max(OPENCODE_TIMEOUT + 30.0, 60.0),
        metadata={
            "kind": "opencode-llm-agent",
            "opencode_url": OPENCODE_URL,
            "opencode_dir": OPENCODE_DIR,
            "opencode_model": OPENCODE_MODEL,
        },
    )

    STATE_PATH = os.path.join(
        os.path.expanduser(
            os.getenv("REALM_STATE_DIR",
                      os.path.join("~", ".local", "share", AGENT_ID, "state"))
        ),
        f"{USERNAME}.json",
    )

    def _read_state() -> dict[str, Any]:
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                return json.loads(fh.read())
        except Exception:
            return {"agent": USERNAME, "state": "idle", "error": "state file not found"}

    def _is_cancel_msg(payload: Any) -> bool:
        if isinstance(payload, dict):
            t = str(payload.get("text") or "")
            return t.strip().upper().startswith("CANCEL")
        if isinstance(payload, str):
            return payload.strip().upper().startswith("CANCEL")
        return False

    def _extract_cancel_task_id(payload: Any) -> str:
        # explicit task_id in dict payload wins
        if isinstance(payload, dict):
            tid = str(payload.get("task_id") or "").strip()
            if tid:
                return tid
        # fallback: regex from text field or raw string
        t = ""
        if isinstance(payload, dict):
            t = str(payload.get("text") or "")
        elif isinstance(payload, str):
            t = payload
        m = re.search(r"task_id=(\S+)", t, re.IGNORECASE)
        return m.group(1) if m else ""

    _TEAM_USERS = frozenset({
        "eng-m2", "m4-dl",
        "medusa-bridge", "m2-opencode-mcp",
        "acct_01kte0xax29t2nkx5smpkt5aw4",  # eng-m2
        "acct_01ktdcv4wsjrpdzp0mmpxj6kj2",  # m2-opencode-mcp
        "acct_01ktczjxzaj2zf0pj5z45h1pzj",  # medusa-bridge
    })

    def _is_from_teammate(msg) -> bool:
        from_id = str(msg.from_account_id or "")
        from_agent = str(msg.from_agent or "")
        return (from_id in _TEAM_USERS or from_agent in _TEAM_USERS)

    @sdk.receive
    async def handle_message(msg) -> None:
        text = extract_text_payload(msg.payload)
        task_id = ""
        if isinstance(msg.payload, dict):
            task_id = str(msg.payload.get("task_id") or "")

        to_agent = msg.from_account_id or msg.from_agent or ""
        should_process = msg.kind == "request" or (msg.kind == "direct" and _is_from_teammate(msg))

        # -- CANCEL (any kind, handled immediately) -----------------------------
        if _is_cancel_msg(msg.payload):
            cancel_id = _extract_cancel_task_id(msg.payload)
            current = _read_state()
            current_id = str(current.get("task_id") or "")
            if cancel_id and cancel_id == current_id:
                await update_agent_state("cancelled", task_id=cancel_id,
                                         error="cancelled by coordinator")
                await sdk.send_text(to_agent,
                    json.dumps({"type": "status", "subtype": "cancelled",
                                "task_id": cancel_id, "text": "Task cancelled"}),
                    thread_id=msg.thread_id)
            return

        # -- STATE query (any kind, handled immediately) ------------------------
        if text.strip().upper() == "STATE":
            state_data = _read_state()
            if msg.kind == "request":
                await sdk.node.reply(msg, {
                    "text": json.dumps({"type": "state", "data": state_data}),
                    "agent": USERNAME,
                    "status": "answered",
                }, thread_id=msg.thread_id)
            elif to_agent:
                await sdk.send_text(to_agent,
                    json.dumps({"type": "state", "data": state_data}),
                    thread_id=msg.thread_id)
            return

        # -- non-processable messages -------------------------------------------
        if not should_process:
            print(f"received {msg.kind} from {to_agent}: {text}", flush=True)
            return

        task_id = task_id or msg.trace_id or ""

        # -- cancellation pre-check --------------------------------------------
        current = _read_state()
        current_state = str(current.get("state") or "")
        current_id = str(current.get("task_id") or "")
        if current_state == "cancelling" and current_id == task_id:
            await update_agent_state("cancelled", task_id=task_id,
                                     error="cancelled before start")
            await sdk.node.reply(msg, {
                "text": json.dumps({"type": "status", "subtype": "cancelled",
                                    "task_id": task_id, "text": "Task cancelled"}),
                "agent": USERNAME,
                "status": "answered",
            }, thread_id=msg.thread_id)
            return

        # -- ACK ----------------------------------------------------------------
        await update_agent_state("acknowledged", task_id=task_id,
                                 thread_id=msg.thread_id,
                                 last_action=text.strip()[:200])
        if to_agent:
            try:
                await sdk.send_text(to_agent,
                    json.dumps({"type": "progress", "subtype": "status",
                                "task_id": task_id,
                                "text": f"ACK received: {text.strip()[:150]}",
                                "visible_by_default": True}),
                    thread_id=msg.thread_id)
            except Exception:
                pass

        # -- WORKING ------------------------------------------------------------
        await update_agent_state("working", task_id=task_id,
                                 thread_id=msg.thread_id)
        if to_agent:
            try:
                await sdk.send_text(to_agent,
                    json.dumps({"type": "progress", "subtype": "status",
                                "task_id": task_id,
                                "text": f"WORKING: {text.strip()[:150]}",
                                "visible_by_default": True}),
                    thread_id=msg.thread_id)
            except Exception:
                pass

        try:
            answer = await ask_opencode(
                f"{SYSTEM_PROMPT}\n\nRequest:\n{text}",
                realm_thread_id=msg.thread_id,
                session_map=session_map,
                sdk=sdk,
                to_agent=to_agent,
                thread_id=msg.thread_id,
            )
            await update_agent_state("done", task_id=task_id,
                                     last_action=answer[:200])
            payload = {
                "text": answer,
                "agent": USERNAME,
                "status": "answered",
                "model": OPENCODE_MODEL,
                "task_id": task_id if task_id else msg.trace_id or "",
            }
        except Exception as exc:  # noqa: BLE001 - returned to requester
            await update_agent_state("failed", task_id=task_id,
                                     error=str(exc)[:500])
            payload = {
                "text": f"OpenCode handler failed: {exc}",
                "agent": USERNAME,
                "status": "error",
                "model": OPENCODE_MODEL,
                "task_id": task_id if task_id else msg.trace_id or "",
            }

        await sdk.node.reply(msg, payload, thread_id=msg.thread_id)

    await sdk.start()
    print(f"{USERNAME} online as {sdk.account_id}", flush=True)

    try:
        await stop.wait()
    finally:
        await sdk.stop()


if __name__ == "__main__":
    asyncio.run(main())
