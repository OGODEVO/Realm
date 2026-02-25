"""CLI for AgentNet — with Rich styling."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from nats.aio.client import Client as NATS
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from agentnet.config import DEFAULT_NATS_URL
from agentnet.node import AgentNode
from agentnet.registry import get_profile, get_thread_status, list_online_agents, search_profiles
from agentnet.utils import decode_json, new_ulid

# ──────────────────────────────────────────────
# Theme + console
# ──────────────────────────────────────────────

_THEME = Theme(
    {
        "agent.name": "bold cyan",
        "agent.id": "dim",
        "agent.online": "bold green",
        "agent.cap": "magenta",
        "agent.meta": "dim yellow",
        "msg.you": "bold white",
        "msg.agent": "bold cyan",
        "msg.system": "dim italic",
        "msg.error": "bold red",
        "field.label": "bold white",
        "field.value": "white",
        "success": "bold green",
        "warn": "bold yellow",
        "info": "bold blue",
        "accent": "bold magenta",
    }
)

console = Console(theme=_THEME, highlight=False)
err_console = Console(theme=_THEME, stderr=True, highlight=False)

BANNER = Text.from_markup(
    "[bold cyan]⬡[/] [bold white]AgentNet[/] [dim]·[/] [dim italic]agent mesh network[/]"
)


def _print_error(message: str) -> None:
    err_console.print(
        Panel(
            Text(message, style="msg.error"),
            border_style="red",
            title="[bold red]Error[/]",
            title_align="left",
            padding=(0, 1),
        )
    )


def _capability_tags(caps: list[str]) -> Text:
    t = Text()
    for i, cap in enumerate(caps):
        if i > 0:
            t.append(" ", style="")
        t.append(f" {cap} ", style="on #3b2d6b white")
    return t


def _time_ago(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    try:
        from datetime import UTC, datetime

        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(UTC) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:  # noqa: BLE001
        return iso_str or "—"


# ──────────────────────────────────────────────
# list
# ──────────────────────────────────────────────

async def _run_list(nats_url: str, timeout: float) -> int:
    with console.status("[info]Connecting to NATS…[/]", spinner="dots"):
        agents = await list_online_agents(nats_url=nats_url, timeout=timeout)

    if not agents:
        console.print(
            Panel(
                "[dim]No agents currently online[/]",
                border_style="dim",
                title="[info]Online Agents[/]",
                title_align="left",
                padding=(0, 1),
            )
        )
        return 0

    table = Table(
        title=f"[info]Online Agents[/]  [dim]({len(agents)})[/]",
        border_style="bright_black",
        title_justify="left",
        show_edge=True,
        pad_edge=True,
        expand=True,
    )
    table.add_column("", width=2, justify="center")  # status dot
    table.add_column("Name", style="agent.name", no_wrap=True)
    table.add_column("Username", style="dim cyan")
    table.add_column("Account ID", style="agent.id", max_width=20)
    table.add_column("Capabilities")
    table.add_column("Last Seen", style="dim", justify="right")

    for agent in agents:
        dot = Text("●", style="agent.online")
        caps = _capability_tags(agent.capabilities)
        acct = (agent.account_id or "—")[:20]
        table.add_row(
            dot,
            agent.name,
            f"@{agent.username}" if agent.username else "—",
            acct,
            caps,
            _time_ago(agent.last_seen),
        )

    console.print(table)
    return 0


# ──────────────────────────────────────────────
# search
# ──────────────────────────────────────────────

async def _run_search(
    nats_url: str,
    query: str,
    capability: str | None,
    limit: int,
    online_only: bool,
    timeout: float,
) -> int:
    with console.status("[info]Searching…[/]", spinner="dots"):
        results = await search_profiles(
            nats_url,
            query=query,
            capability=capability,
            limit=limit,
            online_only=online_only,
            timeout=timeout,
        )

    if not results:
        console.print("[dim]No results found.[/]")
        return 0

    search_label = f'[info]Search Results[/]  [dim]query="[/][accent]{escape(query)}[/][dim]"[/]' if query else "[info]Search Results[/]"
    table = Table(
        title=f"{search_label}  [dim]({len(results)})[/]",
        border_style="bright_black",
        title_justify="left",
        show_edge=True,
        expand=True,
    )
    table.add_column("", width=2, justify="center")
    table.add_column("Name", style="agent.name", no_wrap=True)
    table.add_column("Username", style="dim cyan")
    table.add_column("Account ID", style="agent.id", max_width=20)
    table.add_column("Capabilities")
    table.add_column("Status", justify="center")

    for entry in results:
        is_online = entry.get("online", False)
        dot = Text("●", style="agent.online") if is_online else Text("○", style="dim")
        name = str(entry.get("display_name") or entry.get("name") or entry.get("username") or "?")
        username = entry.get("username") or "—"
        acct = (str(entry.get("account_id") or "—"))[:20]
        caps_raw = entry.get("capabilities") or []
        if isinstance(caps_raw, list):
            caps = _capability_tags([str(c) for c in caps_raw])
        else:
            caps = Text("—", style="dim")
        status_text = Text("online", style="agent.online") if is_online else Text("offline", style="dim")
        table.add_row(dot, name, f"@{username}" if username != "—" else "—", acct, caps, status_text)

    console.print(table)
    return 0


# ──────────────────────────────────────────────
# profile
# ──────────────────────────────────────────────

async def _run_profile(
    nats_url: str,
    account_id: str | None,
    username: str | None,
    timeout: float,
) -> int:
    with console.status("[info]Fetching profile…[/]", spinner="dots"):
        profile = await get_profile(
            nats_url,
            account_id=account_id,
            username=username,
            timeout=timeout,
        )

    display_name = str(profile.get("display_name") or profile.get("name") or profile.get("username") or "Unknown")
    uname = profile.get("username") or "—"
    acct_id = profile.get("account_id") or "—"
    bio = profile.get("bio") or ""
    caps = profile.get("capabilities") or []
    meta = profile.get("metadata") or {}
    visibility = profile.get("visibility") or "—"
    status = profile.get("status") or "—"
    created = profile.get("created_at") or "—"

    # Build profile content
    lines = Text()
    lines.append("Username    ", style="field.label")
    lines.append(f"@{uname}\n", style="dim cyan")
    lines.append("Account     ", style="field.label")
    lines.append(f"{acct_id}\n", style="agent.id")
    if bio:
        lines.append("Bio         ", style="field.label")
        lines.append(f"{bio}\n", style="field.value")
    lines.append("Visibility  ", style="field.label")
    lines.append(f"{visibility}\n", style="field.value")
    lines.append("Status      ", style="field.label")
    is_online = str(status).lower() in ("online", "active")
    lines.append(f"{status}\n", style="agent.online" if is_online else "dim")
    lines.append("Created     ", style="field.label")
    lines.append(f"{created}\n", style="dim")

    if caps:
        lines.append("\nCapabilities  ", style="field.label")
        lines.append_text(_capability_tags([str(c) for c in caps]))
        lines.append("\n")

    if meta:
        lines.append("\nMetadata\n", style="field.label")
        for key, value in meta.items():
            lines.append(f"  {key}: ", style="dim yellow")
            lines.append(f"{value}\n", style="field.value")

    panel = Panel(
        lines,
        title=f"[agent.name]{escape(display_name)}[/]",
        title_align="left",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)
    return 0


# ──────────────────────────────────────────────
# thread-status
# ──────────────────────────────────────────────

async def _run_thread_status(
    nats_url: str,
    thread_id: str,
    soft_limit_tokens: int | None,
    hard_limit_tokens: int | None,
    timeout: float,
) -> int:
    with console.status("[info]Fetching thread status…[/]", spinner="dots"):
        status = await get_thread_status(
            nats_url,
            thread_id=thread_id,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            timeout=timeout,
        )

    state = str(status.get("status") or "unknown")
    state_style = "success" if state == "ok" else "warn" if state == "warn" else "msg.error"

    lines = Text()
    lines.append("Thread      ", style="field.label")
    lines.append(f"{status.get('thread_id', thread_id)}\n", style="field.value")
    lines.append("Status      ", style="field.label")
    lines.append(f"{state}\n", style=state_style)
    lines.append("Messages    ", style="field.label")
    lines.append(f"{status.get('message_count', 0)}\n", style="field.value")
    lines.append("Bytes       ", style="field.label")
    lines.append(f"{status.get('byte_count', 0)}\n", style="field.value")
    lines.append("Tokens~     ", style="field.label")
    lines.append(f"{status.get('approx_tokens', 0)}\n", style="field.value")
    lines.append("Soft/Hard   ", style="field.label")
    lines.append(
        f"{status.get('soft_limit_tokens', '—')} / {status.get('hard_limit_tokens', '—')}\n",
        style="field.value",
    )
    lines.append("Checkpoint  ", style="field.label")
    lines.append(f"{status.get('latest_checkpoint_end', 0)}\n", style="field.value")
    lines.append("Last Msg At ", style="field.label")
    lines.append(f"{status.get('last_message_at') or '—'}\n", style="dim")

    participants = status.get("participants")
    if isinstance(participants, list) and participants:
        lines.append("Participants ", style="field.label")
        lines.append(", ".join(str(item) for item in participants), style="field.value")
        lines.append("\n")

    console.print(
        Panel(
            lines,
            title="[info]Thread Status[/]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    return 0


# ──────────────────────────────────────────────
# send
# ──────────────────────────────────────────────

async def _run_send(
    nats_url: str,
    to_account: str | None,
    to_username: str | None,
    to_capability: str | None,
    payload: str,
    thread_id: str | None,
    parent_message_id: str | None,
    retry_attempts: int,
    receipt_timeout: float,
    no_ack: bool,
) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        _print_error("Payload must be valid JSON")
        return 1

    dest = to_username and f"@{to_username}" or to_account or to_capability or "?"
    with console.status(f"[info]Sending to [accent]{escape(dest)}[/]…[/]", spinner="dots"):
        async with AgentNode(agent_id="cli_sender", name="CLI Sender", nats_url=nats_url) as node:
            if to_capability:
                msg_id = await node.send_to_capability(
                    to_capability,
                    data,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                    require_delivery_ack=not no_ack,
                    retry_attempts=retry_attempts,
                    receipt_timeout=receipt_timeout,
                )
            elif to_account:
                msg_id = await node.send_to_account(
                    to_account,
                    data,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                    require_delivery_ack=not no_ack,
                    retry_attempts=retry_attempts,
                    receipt_timeout=receipt_timeout,
                )
            elif to_username:
                msg_id = await node.send_to_username(
                    to_username,
                    data,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                    require_delivery_ack=not no_ack,
                    retry_attempts=retry_attempts,
                    receipt_timeout=receipt_timeout,
                )
            else:
                _print_error("One destination option is required")
                return 1

    console.print(
        f"[success]✓[/] Message sent to [accent]{escape(dest)}[/]  [dim]msg_id={msg_id}[/]"
    )
    return 0


# ──────────────────────────────────────────────
# request
# ──────────────────────────────────────────────

async def _run_request(
    nats_url: str,
    to_account: str | None,
    to_username: str | None,
    to_capability: str | None,
    payload: str,
    timeout: float,
    thread_id: str | None,
    parent_message_id: str | None,
) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        _print_error("Payload must be valid JSON")
        return 1

    dest = to_username and f"@{to_username}" or to_account or to_capability or "?"
    with console.status(f"[info]Requesting [accent]{escape(dest)}[/]…[/]", spinner="dots"):
        async with AgentNode(agent_id="cli_requester", name="CLI Requester", nats_url=nats_url) as node:
            try:
                if to_capability:
                    reply = await node.request_capability(
                        to_capability,
                        data,
                        timeout=timeout,
                        thread_id=thread_id,
                        parent_message_id=parent_message_id,
                    )
                elif to_account:
                    reply = await node.request_account(
                        to_account,
                        data,
                        timeout=timeout,
                        thread_id=thread_id,
                        parent_message_id=parent_message_id,
                    )
                elif to_username:
                    reply = await node.request_username(
                        to_username,
                        data,
                        timeout=timeout,
                        thread_id=thread_id,
                        parent_message_id=parent_message_id,
                    )
                else:
                    _print_error("One destination option is required")
                    return 1
            except asyncio.TimeoutError:
                _print_error(f"Request timed out — no reply from {dest}")
                return 1

    reply_json = json.dumps(reply.to_dict(), indent=2)
    console.print(
        Panel(
            RichJSON(reply_json),
            title=f"[success]Reply[/] [dim]from {escape(dest)}[/]",
            title_align="left",
            border_style="green",
            padding=(0, 1),
        )
    )
    return 0


# ──────────────────────────────────────────────
# watch
# ──────────────────────────────────────────────

async def _run_watch(
    nats_url: str,
    subject: str,
    max_messages: int,
    include_payload: bool,
) -> int:
    nc = NATS()
    with console.status("[info]Connecting to NATS…[/]", spinner="dots"):
        await nc.connect(servers=[nats_url], name="agentnet-cli-watch")

    remaining = max_messages if max_messages > 0 else None
    done = asyncio.Event()
    count = 0

    console.print(Rule(f"[info]Watching[/]  [accent]{escape(subject)}[/]"))
    if remaining is None:
        console.print("[dim]Press Ctrl+C to stop[/]\n")

    async def _handle(msg) -> None:
        nonlocal remaining, count
        count += 1
        try:
            payload = decode_json(msg.data)
        except Exception:  # noqa: BLE001
            payload = msg.data.decode("utf-8", errors="replace")

        if isinstance(payload, dict):
            kind = payload.get("kind", "—")
            from_agent = payload.get("from_agent", "—")
            to_agent = payload.get("to_agent", "—")
            msg_id = payload.get("message_id", "")[:12]
            thread_id = payload.get("thread_id", "")

            line = Text()
            line.append(f"#{count:<4} ", style="dim")
            line.append(f"{msg.subject} ", style="accent")

            kind_style = "success" if kind in ("direct", "reply") else "info"
            line.append(f" {kind} ", style=f"on {kind_style} bold")
            line.append("  ", style="")

            line.append(f"{from_agent}", style="agent.name")
            line.append(" → ", style="dim")
            line.append(f"{to_agent}", style="agent.name")

            if msg_id:
                line.append(f"  [dim]id={msg_id}…[/]")
            if thread_id:
                line.append(f"  [dim]thread={thread_id[:16]}…[/]")

            console.print(line)

            if include_payload:
                p = payload.get("payload")
                if p:
                    console.print(
                        Panel(
                            RichJSON(json.dumps(p, indent=2)),
                            border_style="dim",
                            padding=(0, 1),
                        )
                    )
        else:
            line = Text()
            line.append(f"#{count:<4} ", style="dim")
            line.append(f"{msg.subject} ", style="accent")
            if include_payload:
                line.append(str(payload), style="field.value")
            else:
                line.append("<non-json>", style="dim")
            console.print(line)

        if remaining is not None:
            remaining -= 1
            if remaining <= 0:
                done.set()

    try:
        await nc.subscribe(subject, cb=_handle)
        if remaining is None:
            await asyncio.Event().wait()
        else:
            await done.wait()
    finally:
        if nc.is_connected:
            await nc.drain()

    console.print(Rule("[dim]stream ended[/]"))
    return 0


# ──────────────────────────────────────────────
# chat
# ──────────────────────────────────────────────

async def _run_chat(
    nats_url: str,
    to_account: str | None,
    to_username: str | None,
    to_capability: str | None,
    timeout: float,
    thread_id: str | None,
    parent_message_id: str | None,
    raw: bool,
) -> int:
    current_thread = thread_id or f"thread_cli_{new_ulid().lower()}"
    dest = to_username and f"@{to_username}" or to_account or to_capability or "?"

    console.print()
    console.print(
        Panel(
            Text.from_markup(
                f"[agent.name]{escape(dest)}[/]\n"
                f"[dim]thread [/][dim italic]{current_thread}[/]\n\n"
                f"[dim]/quit  /thread <id>  /showthread[/]"
            ),
            title="[info]Chat Session[/]",
            title_align="left",
            border_style="cyan",
            padding=(0, 2),
        )
    )
    console.print()

    async with AgentNode(agent_id="cli_chat", name="CLI Chat", nats_url=nats_url) as node:
        while True:
            try:
                line = await asyncio.to_thread(
                    console.input, "[bold white]  » [/]"
                )
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0
            text = line.strip()
            if not text:
                continue
            if text == "/quit":
                console.print("[dim]Bye.[/]")
                return 0
            if text == "/showthread":
                console.print(f"  [dim]thread_id=[/][accent]{current_thread}[/]")
                continue
            if text.startswith("/thread "):
                new_thread = text.split(" ", 1)[1].strip()
                if not new_thread:
                    console.print("  [warn]thread id cannot be empty[/]")
                    continue
                current_thread = new_thread
                console.print(f"  [dim]thread_id=[/][accent]{current_thread}[/]")
                continue

            payload = {"text": text}
            try:
                if to_capability:
                    reply = await node.request_capability(
                        to_capability,
                        payload,
                        timeout=timeout,
                        thread_id=current_thread,
                        parent_message_id=parent_message_id,
                    )
                elif to_account:
                    reply = await node.request_account(
                        to_account,
                        payload,
                        timeout=timeout,
                        thread_id=current_thread,
                        parent_message_id=parent_message_id,
                    )
                elif to_username:
                    reply = await node.request_username(
                        to_username,
                        payload,
                        timeout=timeout,
                        thread_id=current_thread,
                        parent_message_id=parent_message_id,
                    )
                else:
                    _print_error("Destination required")
                    return 1
            except asyncio.TimeoutError:
                console.print("  [msg.error]⏱  timeout — no reply[/]")
                continue

            if raw:
                reply_json = json.dumps(reply.to_dict(), indent=2)
                console.print(
                    Panel(
                        RichJSON(reply_json),
                        border_style="cyan",
                        padding=(0, 1),
                    )
                )
            else:
                out = reply.payload if isinstance(reply.payload, dict) else {"text": str(reply.payload)}
                text_out = str(out.get("text") or out.get("error") or "").strip() or json.dumps(out)
                console.print(
                    Panel(
                        Text(text_out, style="field.value"),
                        title=f"[msg.agent]{escape(dest)}[/]",
                        title_align="left",
                        border_style="cyan",
                        padding=(0, 2),
                    )
                )
            current_thread = reply.thread_id or current_thread


# ──────────────────────────────────────────────
# Argument parsing + main
# ──────────────────────────────────────────────

class _RichHelpFormatter(argparse.HelpFormatter):
    """Just widen the help output a bit."""

    def __init__(self, prog: str, **kwargs) -> None:
        kwargs.setdefault("max_help_position", 36)
        kwargs.setdefault("width", 88)
        super().__init__(prog, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agentnet",
        description="AgentNet CLI",
        formatter_class=_RichHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List currently online agents", formatter_class=_RichHelpFormatter)
    list_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    list_parser.add_argument("--timeout", type=float, default=2.0)

    search_parser = subparsers.add_parser("search", help="Search agent profiles", formatter_class=_RichHelpFormatter)
    search_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    search_parser.add_argument("--query", default="")
    search_parser.add_argument("--capability")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--online-only", action="store_true")
    search_parser.add_argument("--timeout", type=float, default=2.0)

    profile_parser = subparsers.add_parser("profile", help="Get account profile", formatter_class=_RichHelpFormatter)
    profile_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    profile_group = profile_parser.add_mutually_exclusive_group(required=True)
    profile_group.add_argument("--account-id")
    profile_group.add_argument("--username")
    profile_parser.add_argument("--timeout", type=float, default=2.0)

    thread_parser = subparsers.add_parser(
        "thread-status",
        help="Inspect thread size/status for compaction decisions",
        formatter_class=_RichHelpFormatter,
    )
    thread_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    thread_parser.add_argument("--thread-id", required=True, help="Thread ID to inspect")
    thread_parser.add_argument("--soft-limit-tokens", type=int, help="Optional soft threshold override")
    thread_parser.add_argument("--hard-limit-tokens", type=int, help="Optional hard threshold override")
    thread_parser.add_argument("--timeout", type=float, default=2.0)

    send_parser = subparsers.add_parser("send", help="Send a message", formatter_class=_RichHelpFormatter)
    send_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    group = send_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--to-account", help="Account ID to send to")
    group.add_argument("--to-username", help="Username to send to")
    group.add_argument("--to-capability", help="Capability to send to")
    send_parser.add_argument("--thread-id", help="Thread ID to attach")
    send_parser.add_argument("--parent-message-id", help="Parent message ID")
    send_parser.add_argument("--retry-attempts", type=int, default=2, help="Retries after first publish")
    send_parser.add_argument("--receipt-timeout", type=float, default=1.5, help="Seconds to wait per attempt")
    send_parser.add_argument("--no-ack", action="store_true", help="Disable delivery-ack wait")
    send_parser.add_argument("payload", help="JSON payload string")

    req_parser = subparsers.add_parser("request", help="Send an RPC request and await a reply", formatter_class=_RichHelpFormatter)
    req_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    req_parser.add_argument("--timeout", type=float, default=5.0)
    req_group = req_parser.add_mutually_exclusive_group(required=True)
    req_group.add_argument("--to-account", help="Account ID to request")
    req_group.add_argument("--to-username", help="Username to request")
    req_group.add_argument("--to-capability", help="Capability to request")
    req_parser.add_argument("--thread-id", help="Thread ID to attach")
    req_parser.add_argument("--parent-message-id", help="Parent message ID")
    req_parser.add_argument("payload", help="JSON payload string")

    watch_parser = subparsers.add_parser("watch", help="Watch live network subjects", formatter_class=_RichHelpFormatter)
    watch_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    watch_parser.add_argument("--subject", default="account.*.inbox", help="NATS subject pattern")
    watch_parser.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Stop after N messages (0 = Ctrl+C)",
    )
    watch_parser.add_argument("--include-payload", action="store_true", help="Include payload field in output")

    chat_parser = subparsers.add_parser("chat", help="Interactive chat with an agent", formatter_class=_RichHelpFormatter)
    chat_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    chat_parser.add_argument("--timeout", type=float, default=60.0)
    chat_group = chat_parser.add_mutually_exclusive_group(required=True)
    chat_group.add_argument("--to-account", help="Account ID to chat with")
    chat_group.add_argument("--to-username", help="Username to chat with")
    chat_group.add_argument("--to-capability", help="Capability to chat with")
    chat_parser.add_argument("--thread-id", help="Initial thread ID to use")
    chat_parser.add_argument("--parent-message-id", help="Optional parent message ID")
    chat_parser.add_argument("--raw", action="store_true", help="Print full reply envelope")

    # If no args, print styled help
    if len(sys.argv) == 1:
        console.print()
        console.print(BANNER)
        console.print()
        parser.print_help()
        return 0

    args = parser.parse_args()

    try:
        if args.command == "list":
            return asyncio.run(_run_list(nats_url=args.nats_url, timeout=args.timeout))
        if args.command == "search":
            return asyncio.run(
                _run_search(
                    nats_url=args.nats_url,
                    query=args.query,
                    capability=args.capability,
                    limit=args.limit,
                    online_only=args.online_only,
                    timeout=args.timeout,
                )
            )
        if args.command == "profile":
            return asyncio.run(
                _run_profile(
                    nats_url=args.nats_url,
                    account_id=args.account_id,
                    username=args.username,
                    timeout=args.timeout,
                )
            )
        if args.command == "thread-status":
            return asyncio.run(
                _run_thread_status(
                    nats_url=args.nats_url,
                    thread_id=args.thread_id,
                    soft_limit_tokens=args.soft_limit_tokens,
                    hard_limit_tokens=args.hard_limit_tokens,
                    timeout=args.timeout,
                )
            )
        if args.command == "send":
            return asyncio.run(
                _run_send(
                    args.nats_url,
                    args.to_account,
                    args.to_username,
                    args.to_capability,
                    args.payload,
                    args.thread_id,
                    args.parent_message_id,
                    args.retry_attempts,
                    args.receipt_timeout,
                    args.no_ack,
                )
            )
        if args.command == "request":
            return asyncio.run(
                _run_request(
                    args.nats_url,
                    args.to_account,
                    args.to_username,
                    args.to_capability,
                    args.payload,
                    args.timeout,
                    args.thread_id,
                    args.parent_message_id,
                )
            )
        if args.command == "watch":
            return asyncio.run(
                _run_watch(
                    nats_url=args.nats_url,
                    subject=args.subject,
                    max_messages=args.max_messages,
                    include_payload=args.include_payload,
                )
            )
        if args.command == "chat":
            return asyncio.run(
                _run_chat(
                    nats_url=args.nats_url,
                    to_account=args.to_account,
                    to_username=args.to_username,
                    to_capability=args.to_capability,
                    timeout=args.timeout,
                    thread_id=args.thread_id,
                    parent_message_id=args.parent_message_id,
                    raw=args.raw,
                )
            )
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        _print_error(str(exc))
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
