#!/usr/bin/env python3
"""Realm agent backed by a headless CLI brain (Codex or Grok).

Keeps a Realm identity online, receives ``task.assign`` messages, runs a
local CLI subprocess (``codex exec`` or ``grok``), streams stdout as
``task.progress``, and finishes with a terminal ``task.result``.

OpenCode is intentionally not fully implemented here — use
``examples/opencode_realm_agent.py`` (and ``start-opencode-agent.sh``)
for OpenCode-backed workers. Set ``REALM_RUNTIME=opencode`` only to get a
clear error pointing at that path.

Environment (common)
--------------------
REALM_NATS_URL, REALM_AGENT_ID, REALM_AGENT_NAME, REALM_USERNAME
REALM_RUNTIME          opencode | codex | grok   (default: codex)
REALM_WORKDIR          working directory for the brain
                       (also honors OPENCODE_DIR for compatibility)
REALM_SYSTEM_PROMPT    optional system preamble
REALM_WORK_TIMEOUT_SECONDS  node handler budget (default 86400)
REALM_BRAIN_TIMEOUT    subprocess timeout seconds (default 86400)

Codex
-----
CODEX_BIN              default: codex
CODEX_MODEL            optional ``-m``
CODEX_SANDBOX          read-only | workspace-write | danger-full-access
                       (default: workspace-write)
CODEX_FULL_AUTO        if truthy, pass --dangerously-bypass-approvals-and-sandbox
CODEX_JSON             if truthy, pass --json
CODEX_SKIP_GIT_CHECK   if truthy, pass --skip-git-repo-check

Grok
----
GROK_BIN               default: grok
GROK_MODEL             optional ``-m``
GROK_ALWAYS_APPROVE    default true — pass --always-approve
GROK_OUTPUT_FORMAT     plain | json | streaming-json (default: plain)
GROK_MODE              agent | single
                       agent  → multi-turn: ``grok [flags] PROMPT`` with output-format
                       single → ``grok -p/--single PROMPT``
                       (default: agent)
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
        build_task_result,
        task_type,
    )
except ModuleNotFoundError:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from agentnet.sdk import AgentSDK
    from agentnet.task_protocol import (
        TASK_ASSIGN,
        build_task_result,
        task_type,
    )

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

SUPPORTED_RUNTIMES = frozenset({"opencode", "codex", "grok"})
TASK_COMPLETE_MARKER = "[REALM_TASK_COMPLETE]"
TASK_BLOCKED_MARKER = "[REALM_TASK_BLOCKED]"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_runtime(raw: str | None, default: str = "codex") -> str:
    value = (raw or default).strip().lower()
    if value not in SUPPORTED_RUNTIMES:
        raise ValueError(
            f"Unsupported REALM_RUNTIME={raw!r}; "
            f"expected one of {sorted(SUPPORTED_RUNTIMES)}"
        )
    return value


def workdir_from_env() -> str:
    return (
        os.getenv("REALM_WORKDIR")
        or os.getenv("OPENCODE_DIR")
        or os.getcwd()
    )


NATS_URL = os.getenv("REALM_NATS_URL", "nats://agentnet_secret_token@localhost:4222")
AGENT_ID = os.getenv("REALM_AGENT_ID", "cli-agent")
AGENT_NAME = os.getenv("REALM_AGENT_NAME", "CLI Agent")
USERNAME = os.getenv("REALM_USERNAME", "cli-agent")
RUNTIME = parse_runtime(os.getenv("REALM_RUNTIME"), default="codex")
WORKDIR = workdir_from_env()
WORK_TIMEOUT = float(os.getenv("REALM_WORK_TIMEOUT_SECONDS", "86400"))
BRAIN_TIMEOUT = float(os.getenv("REALM_BRAIN_TIMEOUT", "86400"))
BLOB_DIR = os.getenv("REALM_BLOB_DIR") or None
PROGRESS_CHUNK = max(40, int(os.getenv("REALM_PROGRESS_CHUNK", "280")))
PROGRESS_INTERVAL_S = float(os.getenv("REALM_PROGRESS_INTERVAL_S", "2.0"))

SYSTEM_PROMPT = os.getenv(
    "REALM_SYSTEM_PROMPT",
    f"You are a Realm agent ({USERNAME}) backed by the {RUNTIME} CLI.",
)
EXECUTION_CONTRACT = (
    "Execution contract: Keep working autonomously until the task is complete "
    "or genuinely blocked on one specific piece of user input. Every terminal "
    "answer MUST end with exactly one status marker: [REALM_TASK_COMPLETE] when "
    "the requested work is finished, or [REALM_TASK_BLOCKED] when user input is "
    "required. Do not emit either marker while work remains."
)

CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_MODEL = os.getenv("CODEX_MODEL", "")
CODEX_SANDBOX = os.getenv("CODEX_SANDBOX", "workspace-write")
CODEX_FULL_AUTO = env_bool("CODEX_FULL_AUTO", default=False)
CODEX_JSON = env_bool("CODEX_JSON", default=False)
CODEX_SKIP_GIT = env_bool("CODEX_SKIP_GIT_CHECK", default=True)

GROK_BIN = os.getenv("GROK_BIN", "grok")
GROK_MODEL = os.getenv("GROK_MODEL", "")
GROK_ALWAYS_APPROVE = env_bool("GROK_ALWAYS_APPROVE", default=True)
GROK_OUTPUT_FORMAT = os.getenv("GROK_OUTPUT_FORMAT", "plain").strip().lower() or "plain"
GROK_MODE = os.getenv("GROK_MODE", "agent").strip().lower() or "agent"

STATE_DIR_DEFAULT = os.path.join(
    os.path.expanduser("~"), ".local", "share", AGENT_ID, "state",
)
STATE_DIR = os.getenv("REALM_STATE_DIR", STATE_DIR_DEFAULT)
STATE_PATH = os.path.join(STATE_DIR, f"{USERNAME}.json")
AGENT_STATE_BIN = os.getenv(
    "AGENT_STATE_SCRIPT",
    os.path.expanduser("~/.local/bin/agent-state-update"),
)


# ---------------------------------------------------------------------------
# Pure helpers (testable without NATS)
# ---------------------------------------------------------------------------

def build_codex_cmd(
    prompt: str,
    *,
    bin_path: str = CODEX_BIN,
    workdir: str = WORKDIR,
    model: str = CODEX_MODEL,
    sandbox: str = CODEX_SANDBOX,
    full_auto: bool = CODEX_FULL_AUTO,
    json_events: bool = CODEX_JSON,
    skip_git_check: bool = CODEX_SKIP_GIT,
) -> list[str]:
    """Build ``codex exec`` argv for a headless run."""
    cmd = [bin_path, "exec"]
    if full_auto:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    elif sandbox:
        cmd.extend(["-s", sandbox])
    if workdir:
        cmd.extend(["-C", workdir])
    if model:
        cmd.extend(["-m", model])
    if json_events:
        cmd.append("--json")
    if skip_git_check:
        cmd.append("--skip-git-repo-check")
    cmd.append(prompt)
    return cmd


def build_grok_cmd(
    prompt: str,
    *,
    bin_path: str = GROK_BIN,
    workdir: str = WORKDIR,
    model: str = GROK_MODEL,
    always_approve: bool = GROK_ALWAYS_APPROVE,
    output_format: str = GROK_OUTPUT_FORMAT,
    mode: str = GROK_MODE,
) -> list[str]:
    """Build a headless ``grok`` argv.

    ``grok agent`` is a transport/mode switch (stdio/headless/serve), not a
    prompt runner. Multi-turn headless uses the top-level CLI with a prompt
    plus ``--output-format``. Single-turn uses ``-p/--single``.
    """
    cmd = [bin_path]
    if always_approve:
        cmd.append("--always-approve")
    if workdir:
        cmd.extend(["--cwd", workdir])
    if model:
        cmd.extend(["-m", model])
    if output_format:
        cmd.extend(["--output-format", output_format])
    mode_n = (mode or "agent").strip().lower()
    if mode_n in {"single", "p", "prompt"}:
        cmd.extend(["-p", prompt])
    else:
        # Multi-turn agentic headless (prompt as positional).
        cmd.append(prompt)
    return cmd


def build_brain_cmd(runtime: str, prompt: str) -> list[str]:
    runtime_n = parse_runtime(runtime)
    if runtime_n == "opencode":
        raise RuntimeError(
            "REALM_RUNTIME=opencode is not implemented in cli_realm_agent.py. "
            "Use examples/opencode_realm_agent.py with "
            "services/agent-template/start-opencode-agent.sh instead."
        )
    if runtime_n == "codex":
        return build_codex_cmd(prompt)
    if runtime_n == "grok":
        return build_grok_cmd(prompt)
    raise ValueError(f"Unsupported runtime: {runtime_n}")


def build_task_prompt(
    text: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    execution_contract: str = EXECUTION_CONTRACT,
    task_id: str = "",
    task_title: str = "",
    parent_task_id: str = "",
    thread_id: str = "",
    metadata: dict[str, Any] | None = None,
    include_task_context: bool = True,
) -> str:
    parts = [system_prompt.strip(), "", execution_contract.strip()]
    if include_task_context:
        parts.extend(
            [
                "",
                "Realm task context:",
                f"- task_id: {task_id or '(none)'}",
                f"- parent_task_id: {parent_task_id or '(none)'}",
                f"- title: {task_title or '(none)'}",
                f"- thread_id: {thread_id or '(none)'}",
                f"- metadata: {json.dumps(metadata or {}, sort_keys=True)}",
            ]
        )
    parts.extend(["", "Request:", text])
    return "\n".join(parts)


def task_answer_status(answer: str) -> str | None:
    has_complete = TASK_COMPLETE_MARKER in answer
    has_blocked = TASK_BLOCKED_MARKER in answer
    if has_complete == has_blocked:
        return None
    return "complete" if has_complete else "blocked"


def clean_task_answer(answer: str) -> str:
    return (
        str(answer or "")
        .replace(TASK_COMPLETE_MARKER, "")
        .replace(TASK_BLOCKED_MARKER, "")
        .strip()
    )


def extract_text_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("text") or "")
    if isinstance(payload, str):
        return payload
    return ""


def _clip(text: str, limit: int = 400) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def classify_progress_line(line: str) -> tuple[str, str]:
    """Map a brain stdout line to (phase, text) for task.progress."""
    stripped = line.strip()
    if not stripped:
        return "text", ""
    # Codex --json / Grok streaming-json lines
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return "text", _clip(stripped, PROGRESS_CHUNK)
        if not isinstance(event, dict):
            return "text", _clip(stripped, PROGRESS_CHUNK)
        etype = str(
            event.get("type")
            or event.get("event")
            or event.get("kind")
            or ""
        ).lower()
        # Common shapes across CLIs
        if "tool" in etype or etype in {"function_call", "function_call_output"}:
            item = event.get("item")
            item_name = item.get("name") if isinstance(item, dict) else None
            name = (
                event.get("name")
                or event.get("tool")
                or event.get("tool_name")
                or item_name
                or "tool"
            )
            return "tool", _clip(str(name), PROGRESS_CHUNK)
        text = (
            event.get("text")
            or event.get("message")
            or event.get("content")
            or event.get("delta")
            or ""
        )
        if isinstance(text, dict):
            text = text.get("text") or json.dumps(text, default=str)
        if etype in {"reasoning", "thinking"}:
            return "thinking", _clip(str(text or etype), PROGRESS_CHUNK)
        if text:
            return "text", _clip(str(text), PROGRESS_CHUNK)
        return "status", _clip(etype or stripped, PROGRESS_CHUNK)
    lower = stripped.lower()
    if lower.startswith("tool") or " running " in lower or lower.startswith("$ "):
        return "tool", _clip(stripped, PROGRESS_CHUNK)
    return "text", _clip(stripped, PROGRESS_CHUNK)


# ---------------------------------------------------------------------------
# State / cancel
# ---------------------------------------------------------------------------

_CANCEL_RE = re.compile(r"task_id=(\S+)", re.IGNORECASE)


def read_agent_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.loads(fh.read())
    except Exception:
        return {"agent": USERNAME, "state": "idle"}


async def update_agent_state(state: str, **extra: str) -> None:
    args = [AGENT_STATE_BIN, "--agent", USERNAME, "--state", state]
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
        pass


def is_cancel_msg(payload: Any) -> bool:
    return extract_text_payload(payload).strip().upper().startswith("CANCEL")


def extract_cancel_task_id(payload: Any) -> str:
    if isinstance(payload, dict):
        tid = str(payload.get("task_id") or "").strip()
        if tid:
            return tid
    m = _CANCEL_RE.search(extract_text_payload(payload))
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Progress + subprocess
# ---------------------------------------------------------------------------

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
                json.dumps(
                    {
                        "type": "progress",
                        "subtype": phase,
                        "text": body,
                        "visible_by_default": True,
                    }
                ),
                thread_id=thread_id or None,
            )
    except Exception:
        pass


async def stop_subprocess(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def run_brain(
    cmd: list[str],
    *,
    sdk: AgentSDK | None = None,
    to_agent: str = "",
    thread_id: str = "",
    task_id: str = "",
    timeout: float = BRAIN_TIMEOUT,
    cwd: str | None = None,
) -> str:
    """Run the brain CLI, streaming stdout lines as progress; return full stdout."""
    print(f"brain: {' '.join(cmd[:8])}{' …' if len(cmd) > 8 else ''}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or WORKDIR or None,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    last_progress = 0.0
    loop = asyncio.get_running_loop()

    async def _pump(
        stream: asyncio.StreamReader,
        sink: list[str],
        *,
        as_progress: bool,
    ) -> None:
        nonlocal last_progress
        while True:
            line_b = await stream.readline()
            if not line_b:
                break
            line = line_b.decode("utf-8", errors="replace")
            sink.append(line)
            if not as_progress or not task_id:
                continue
            now = loop.time()
            phase, text = classify_progress_line(line)
            if not text:
                continue
            # Throttle noisy streams but always emit tool lines promptly.
            if phase != "tool" and (now - last_progress) < PROGRESS_INTERVAL_S:
                continue
            last_progress = now
            await emit_progress(
                sdk,
                to_agent,
                thread_id=thread_id,
                task_id=task_id,
                text=text,
                phase=phase if phase != "thinking" else "status",
                metadata={"source": f"{RUNTIME}-cli"},
            )

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _pump(proc.stdout, stdout_chunks, as_progress=True),
                _pump(proc.stderr, stderr_chunks, as_progress=False),
                proc.wait(),
            ),
            timeout=timeout,
        )
    except TimeoutError as exc:
        await stop_subprocess(proc)
        raise RuntimeError(f"{RUNTIME} timed out after {timeout:g} seconds") from exc
    except asyncio.CancelledError:
        await stop_subprocess(proc)
        raise

    stdout = "".join(stdout_chunks).strip()
    stderr = "".join(stderr_chunks).strip()
    if proc.returncode != 0:
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise RuntimeError(f"{RUNTIME} exited {proc.returncode}: {_clip(detail, 800)}")
    return stdout or stderr or f"({RUNTIME} returned no text.)"


def _build_reply(text: str, status: str, task_id: str = "") -> dict[str, Any]:
    return {
        "text": text,
        "agent": USERNAME,
        "status": status,
        "runtime": RUNTIME,
        "task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def handle_message(sdk: AgentSDK, msg: Any) -> None:
    text = extract_text_payload(msg.payload)
    to = msg.from_account_id or msg.from_agent or ""
    task_id = ""
    if isinstance(msg.payload, dict):
        task_id = str(msg.payload.get("task_id") or "")
    incoming_task_type = task_type(msg.payload)
    is_task_assignment = incoming_task_type == TASK_ASSIGN
    should_process = msg.kind == "request" or is_task_assignment

    if is_cancel_msg(msg.payload):
        cancel_id = extract_cancel_task_id(msg.payload)
        current = read_agent_state()
        if cancel_id and cancel_id == str(current.get("task_id") or ""):
            await update_agent_state(
                "cancelled", task_id=cancel_id, error="cancelled by coordinator"
            )
            await sdk.send_text(
                to,
                json.dumps(
                    {
                        "type": "status",
                        "subtype": "cancelled",
                        "task_id": cancel_id,
                        "text": "Task cancelled",
                    }
                ),
                thread_id=msg.thread_id,
            )
        return

    if text.strip().upper() == "STATE":
        state_data = read_agent_state()
        state_json = json.dumps({"type": "state", "data": state_data})
        if msg.kind == "request":
            await sdk.node.reply(
                msg, _build_reply(state_json, "answered"), thread_id=msg.thread_id
            )
        else:
            await sdk.send_text(to, state_json, thread_id=msg.thread_id)
        return

    if not should_process:
        print(f"received {msg.kind} from {to}: {_clip(text, 120)}", flush=True)
        return

    task_id = task_id or msg.trace_id or ""
    task_title = ""
    task_metadata: dict[str, Any] = {}
    parent_task = ""
    if isinstance(msg.payload, dict):
        task_title = str(msg.payload.get("title") or "")
        parent_task = str(msg.payload.get("parent_task_id") or "")
        raw_metadata = msg.payload.get("metadata")
        if isinstance(raw_metadata, dict):
            task_metadata = raw_metadata

    if read_agent_state().get("state") == "cancelling":
        await update_agent_state(
            "cancelled", task_id=task_id, error="cancelled before start"
        )
        await sdk.send_text(
            to,
            json.dumps(
                {
                    "type": "status",
                    "subtype": "cancelled",
                    "task_id": task_id,
                    "text": "Task cancelled",
                }
            ),
            thread_id=msg.thread_id,
        )
        return

    await update_agent_state(
        "acknowledged",
        task_id=task_id,
        thread_id=msg.thread_id or "",
        last_action=text.strip()[:200],
    )
    await emit_progress(
        sdk,
        to,
        thread_id=msg.thread_id or "",
        task_id=task_id,
        text=f"ACK: accepted task ({RUNTIME}) — {_clip(text, 150)}",
        phase="ack",
        percent=1,
    )

    await update_agent_state("working", task_id=task_id, thread_id=msg.thread_id or "")
    await emit_progress(
        sdk,
        to,
        thread_id=msg.thread_id or "",
        task_id=task_id,
        text=f"WORKING: {_clip(text, 150)}",
        phase="working",
        percent=5,
    )

    try:
        prompt = build_task_prompt(
            text,
            task_id=task_id,
            task_title=task_title,
            parent_task_id=parent_task,
            thread_id=msg.thread_id or "",
            metadata=task_metadata,
            include_task_context=is_task_assignment,
        )
        cmd = build_brain_cmd(RUNTIME, prompt)
        raw_answer = await run_brain(
            cmd,
            sdk=sdk,
            to_agent=to,
            thread_id=msg.thread_id or "",
            task_id=task_id,
            timeout=BRAIN_TIMEOUT,
        )
        status_marker = task_answer_status(raw_answer)
        answer = clean_task_answer(raw_answer)
        if status_marker == "blocked":
            terminal_status = "blocked"
        else:
            # Missing marker → treat successful CLI exit as completed so
            # coordinators are not stuck; markers remain preferred.
            terminal_status = "completed"

        await update_agent_state("done", task_id=task_id, last_action=answer[:200])
        payload = (
            build_task_result(
                task_id=task_id,
                text=answer,
                status=terminal_status,
                metadata={
                    "agent": USERNAME,
                    "runtime": RUNTIME,
                    "workdir": WORKDIR,
                },
            )
            if is_task_assignment
            else _build_reply(answer, "answered", task_id)
        )
    except Exception as exc:
        await update_agent_state("failed", task_id=task_id, error=str(exc)[:500])
        payload = (
            build_task_result(
                task_id=task_id,
                text=f"{RUNTIME} handler failed: {exc}",
                status="failed",
                metadata={"agent": USERNAME, "runtime": RUNTIME},
            )
            if is_task_assignment
            else _build_reply(f"{RUNTIME} handler failed: {exc}", "error", task_id)
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
    if RUNTIME == "opencode":
        raise SystemExit(
            "REALM_RUNTIME=opencode is not supported by cli_realm_agent.py.\n"
            "Use examples/opencode_realm_agent.py with "
            "services/agent-template/start-opencode-agent.sh."
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    caps = ["llm", "coding-agent", f"{RUNTIME}-cli", RUNTIME]
    metadata = {
        "kind": f"{RUNTIME}-cli-agent",
        "runtime": RUNTIME,
        "workdir": WORKDIR,
        "emits_task_progress": True,
    }
    if RUNTIME == "codex":
        metadata["codex_bin"] = CODEX_BIN
        metadata["codex_sandbox"] = CODEX_SANDBOX
        metadata["codex_model"] = CODEX_MODEL
        metadata["codex_full_auto"] = CODEX_FULL_AUTO
    elif RUNTIME == "grok":
        metadata["grok_bin"] = GROK_BIN
        metadata["grok_model"] = GROK_MODEL
        metadata["grok_mode"] = GROK_MODE
        metadata["grok_output_format"] = GROK_OUTPUT_FORMAT

    sdk = AgentSDK(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        username=USERNAME,
        capabilities=caps,
        nats_url=NATS_URL,
        blob_store_dir=BLOB_DIR,
        heartbeat_interval=12.0,
        work_timeout_seconds=max(WORK_TIMEOUT, BRAIN_TIMEOUT + 60.0, 60.0),
        metadata=metadata,
    )

    sdk.receive(lambda msg: handle_message(sdk, msg))

    await sdk.start()
    print(
        f"{USERNAME} online as {sdk.account_id} runtime={RUNTIME} workdir={WORKDIR}",
        flush=True,
    )
    try:
        await stop.wait()
    finally:
        await sdk.stop()


if __name__ == "__main__":
    asyncio.run(main())
