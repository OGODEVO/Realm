#!/usr/bin/env python3
"""Realm agent backed by a headless OpenCode server.

This keeps a Realm identity online, receives request messages, forwards each
prompt to OpenCode, and replies on the original request reply subject.

It also maps Realm thread IDs to OpenCode session IDs so follow-up requests in
the same Realm thread continue the same OpenCode conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
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
SESSION_MAP_PATH = Path(
    os.getenv("REALM_OPENCODE_SESSION_MAP", ".realm/opencode_sessions.json")
)


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


async def ask_opencode(
    prompt: str,
    *,
    realm_thread_id: str | None,
    session_map: dict[str, str],
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

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=OPENCODE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"OpenCode timed out after {OPENCODE_TIMEOUT:.0f}s")

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"OpenCode exited {proc.returncode}: {stderr_text}")

    chunks: list[str] = []
    first_session_id: str | None = None

    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        session_id = event.get("sessionID")
        if isinstance(session_id, str) and session_id and first_session_id is None:
            first_session_id = session_id

        if event.get("type") == "text":
            text = event.get("part", {}).get("text")
            if isinstance(text, str):
                chunks.append(text)

    if map_key and first_session_id:
        session_map[map_key] = first_session_id
        save_session_map(session_map)

    answer = "".join(chunks).strip()
    if answer:
        return answer
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
        heartbeat_interval=12.0,
        work_timeout_seconds=max(OPENCODE_TIMEOUT + 30.0, 60.0),
        metadata={
            "kind": "opencode-llm-agent",
            "opencode_url": OPENCODE_URL,
            "opencode_dir": OPENCODE_DIR,
            "opencode_model": OPENCODE_MODEL,
        },
    )

    @sdk.receive
    async def handle_message(msg) -> None:
        text = extract_text_payload(msg.payload)

        if msg.kind != "request":
            print(
                f"received {msg.kind} from {msg.from_account_id or msg.from_agent}: {text}",
                flush=True,
            )
            return

        try:
            answer = await ask_opencode(
                (
                    "You are a Realm agent backed by headless OpenCode. "
                    "Answer the request directly and concisely. "
                    "Use your configured tools when useful.\n\n"
                    f"Request:\n{text}"
                ),
                realm_thread_id=msg.thread_id,
                session_map=session_map,
            )
            payload = {
                "text": answer,
                "agent": USERNAME,
                "status": "answered",
                "model": OPENCODE_MODEL,
            }
        except Exception as exc:  # noqa: BLE001 - returned to requester
            payload = {
                "text": f"OpenCode handler failed: {exc}",
                "agent": USERNAME,
                "status": "error",
                "model": OPENCODE_MODEL,
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
