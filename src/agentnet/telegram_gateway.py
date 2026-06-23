"""Telegram gateway for joining Realm as a human operator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time

from dotenv import load_dotenv

from agentnet.config import DEFAULT_NATS_URL
from agentnet.gateway_core import (
    RenderState,
    SessionStore,
    env_bool,
    extract_text,
    normalize_target,
    payload_type,
    progress_text,
    render_text,
    result_text,
    safe_thread_label,
    same_text,
    stream_text,
)
from agentnet.gateway_telegram import TelegramAPIError, TelegramClient, TelegramPollingConflict
from agentnet.schema import AgentMessage
from agentnet.sdk import AgentRequestError, AgentSDK

LOGGER = logging.getLogger("agentnet.telegram_gateway")
TELEGRAM_EDIT_INTERVAL_SECONDS = 0.8


class TelegramRealmGateway:
    """Wire Telegram chats to Realm threads and agent targets."""

    def __init__(
        self,
        *,
        token: str,
        nats_url: str,
        agent_id: str,
        username: str,
        name: str,
        state_path: str,
        request_timeout: float,
        allowed_chat_ids: set[int] | None = None,
    ) -> None:
        self.telegram = TelegramClient(token)
        self.sdk = AgentSDK(
            agent_id=agent_id,
            username=username,
            name=name,
            capabilities=["human-gateway", "telegram"],
            nats_url=nats_url,
            default_request_timeout=request_timeout,
            default_thread_prefix="thread_tg",
            metadata={"kind": "telegram-gateway"},
        )
        self.store = SessionStore(state_path)
        self.request_timeout = request_timeout
        self.allowed_chat_ids = allowed_chat_ids or set()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._renders_by_thread: dict[str, RenderState] = {}

    async def run(self) -> None:
        await self.store.load()
        self.sdk.receive(self._handle_realm_message)
        await self.sdk.start()
        try:
            await self._poll_loop()
        finally:
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            await self.sdk.stop()
            await self.telegram.close()

    def stop(self) -> None:
        self._stop.set()

    async def _poll_loop(self) -> None:
        offset: int | None = None
        while not self._stop.is_set():
            try:
                updates = await self.telegram.get_updates(offset=offset)
            except TelegramPollingConflict as exc:
                LOGGER.error("%s", exc)
                LOGGER.error("Stop the existing gateway before starting another one: tmux kill-session -t telegram-gateway")
                self.stop()
                break
            except TelegramAPIError as exc:
                LOGGER.warning("%s", exc)
                await asyncio.sleep(3)
                continue
            except Exception:
                LOGGER.exception("Telegram polling failed")
                await asyncio.sleep(3)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                task = asyncio.create_task(self._handle_update(update))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        text = message.get("text")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            await self.telegram.send_message(chat_id, "This chat is not allowed to use this Realm gateway.")
            return
        async with self._chat_lock(chat_id):
            await self._handle_text(chat_id, text.strip())

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _handle_text(self, chat_id: int, text: str) -> None:
        if not text:
            return
        command, _, rest = text.partition(" ")
        if command.startswith("/"):
            await self._handle_command(chat_id, command.split("@", 1)[0].lower(), rest.strip())
            return
        if text.startswith("@") and " " in text:
            target, text = text.split(" ", 1)
            session = await self.store.get(chat_id)
            session.target = normalize_target(target)
            session.thread_id = session.thread_id or self.sdk.new_thread_id()
            await self.store.put(session)
        await self._ask_active_target(chat_id, text)

    async def _handle_command(self, chat_id: int, command: str, rest: str) -> None:
        handlers = {
            "/start": self._send_help,
            "/help": self._send_help,
            "/who": self._send_who,
            "/to": self._set_target,
            "/new": self._new_thread,
            "/thread": self._switch_thread,
            "/threads": self._send_threads,
            "/history": self._send_history,
            "/status": self._send_status,
            "/send": self._send_fire_and_forget,
        }
        handler = handlers.get(command)
        if handler is None:
            await self.telegram.send_message(chat_id, f"Unknown command: {command}\n\n{self._help_text()}")
            return
        await handler(chat_id, rest)

    async def _send_help(self, chat_id: int, _: str = "") -> None:
        await self.telegram.send_message(chat_id, self._help_text())

    def _help_text(self) -> str:
        return (
            "Realm Telegram gateway\n"
            "/who - list online agents\n"
            "/to @agent - choose who normal messages go to\n"
            "/new [name] - start a fresh Realm thread\n"
            "/thread <id> - switch to an existing Realm thread\n"
            "/threads - list recent threads\n"
            "/history [limit] - show active thread messages\n"
            "/status - show current target and thread\n"
            "/send <text> - fire-and-forget to the active target\n"
            "@agent message - send one message to a target and keep it selected"
        )

    async def _send_who(self, chat_id: int, _: str = "") -> None:
        agents = await self.sdk.list_online()
        if not agents:
            await self.telegram.send_message(chat_id, "No agents are online.")
            return
        lines = ["Online agents:"]
        for agent in agents:
            username = f"@{agent.username}" if agent.username else "(no username)"
            caps = ", ".join(agent.capabilities) if agent.capabilities else "no capabilities"
            lines.append(f"{username} - {agent.name} [{caps}]")
        await self.telegram.send_message(chat_id, "\n".join(lines))

    async def _set_target(self, chat_id: int, target: str) -> None:
        if not target:
            await self.telegram.send_message(chat_id, "Usage: /to @agent")
            return
        session = await self.store.get(chat_id)
        session.target = normalize_target(target)
        session.thread_id = session.thread_id or self.sdk.new_thread_id()
        await self.store.put(session)
        await self.telegram.send_message(chat_id, f"Target set to {session.target}\nThread: {session.thread_id}")

    async def _new_thread(self, chat_id: int, label: str) -> None:
        session = await self.store.get(chat_id)
        if label:
            suffix = self.sdk.new_thread_id().rsplit("_", 1)[-1]
            session.thread_id = f"thread_tg_{safe_thread_label(label)}_{suffix}"
        else:
            session.thread_id = self.sdk.new_thread_id()
        session.parent_message_id = None
        await self.store.put(session)
        await self.telegram.send_message(chat_id, f"New thread: {session.thread_id}")

    async def _switch_thread(self, chat_id: int, thread_id: str) -> None:
        if not thread_id:
            await self.telegram.send_message(chat_id, "Usage: /thread <thread_id>")
            return
        session = await self.store.get(chat_id)
        session.thread_id = thread_id
        session.parent_message_id = None
        await self.store.put(session)
        await self.telegram.send_message(chat_id, f"Switched to thread: {thread_id}")

    async def _send_threads(self, chat_id: int, _: str = "") -> None:
        session = await self.store.get(chat_id)
        participant = session.target[1:] if session.target and session.target.startswith("@") else None
        rows = await self.sdk.list_threads(participant_username=participant, limit=10)
        if not rows:
            await self.telegram.send_message(chat_id, "No threads found.")
            return
        lines = ["Recent threads:"]
        for row in rows:
            marker = "*" if row.get("thread_id") == session.thread_id else "-"
            lines.append(
                f"{marker} {row.get('thread_id')} "
                f"msgs={row.get('message_count', 0)} "
                f"last={row.get('last_message_at') or 'unknown'}"
            )
        await self.telegram.send_message(chat_id, "\n".join(lines))

    async def _send_history(self, chat_id: int, limit_text: str) -> None:
        session = await self.store.get(chat_id)
        if not session.thread_id:
            await self.telegram.send_message(chat_id, "No active thread. Use /new or /to @agent first.")
            return
        try:
            limit = max(1, min(25, int(limit_text))) if limit_text else 10
        except ValueError:
            limit = 10
        result = await self.sdk.get_thread_messages(thread_id=session.thread_id, limit=limit)
        rows = result.get("messages")
        if not isinstance(rows, list) or not rows:
            await self.telegram.send_message(chat_id, "No messages found for this thread.")
            return
        lines = [f"History for {session.thread_id}:"]
        for item in rows:
            if not isinstance(item, dict):
                continue
            sender = str(item.get("from_account_id") or item.get("from_agent") or "?")
            lines.append(f"{sender}: {extract_text(item.get('payload'))}")
        await self.telegram.send_message(chat_id, "\n\n".join(lines))

    async def _send_status(self, chat_id: int, _: str = "") -> None:
        session = await self.store.get(chat_id)
        await self.telegram.send_message(
            chat_id,
            "Realm gateway status\n"
            f"Target: {session.target or '(none)'}\n"
            f"Thread: {session.thread_id or '(none)'}\n"
            f"Parent: {session.parent_message_id or '(none)'}\n"
            f"Gateway: @{self.sdk.username}",
        )

    async def _send_fire_and_forget(self, chat_id: int, text: str) -> None:
        session = await self.store.get(chat_id)
        if not session.target:
            await self.telegram.send_message(chat_id, "No target set. Use /to @agent first.")
            return
        if not text:
            await self.telegram.send_message(chat_id, "Usage: /send <text>")
            return
        session.thread_id = session.thread_id or self.sdk.new_thread_id()
        try:
            result = await self.sdk.send_text(
                session.target,
                text,
                thread_id=session.thread_id,
                parent_message_id=session.parent_message_id,
            )
        except Exception as exc:
            await self.telegram.send_message(chat_id, f"Send failed: {exc}")
            return
        session.parent_message_id = result.message_id or session.parent_message_id
        await self.store.put(session)
        await self.telegram.send_message(chat_id, f"Sent.\nThread: {session.thread_id}")

    async def _ask_active_target(self, chat_id: int, text: str) -> None:
        session = await self.store.get(chat_id)
        if not session.target:
            await self.telegram.send_message(chat_id, "No target set. Use /to @agent, or send '@agent your message'.")
            return
        session.thread_id = session.thread_id or self.sdk.new_thread_id()
        await self.store.put(session)
        render = await self._start_render(chat_id, session.target, session.thread_id)
        try:
            result = await self.sdk.ask_text(
                session.target,
                text,
                thread_id=session.thread_id,
                timeout=self.request_timeout,
                parent_message_id=session.parent_message_id,
            )
        except asyncio.TimeoutError:
            await self._finish_render(render, f"Timed out waiting for {session.target}.")
            return
        except AgentRequestError as exc:
            await self._finish_render(render, f"{session.target} returned an error: {exc}")
            return
        except Exception as exc:
            LOGGER.exception("Realm request failed")
            await self._finish_render(render, f"Realm request failed: {exc}")
            return
        session.parent_message_id = result.message_id or session.parent_message_id
        session.thread_id = result.thread_id or session.thread_id
        await self.store.put(session)
        final_text = result_text(result)
        if final_text and not same_text(render.text, final_text):
            await self._finish_render(render, final_text, include_thread=True)
        elif not render.text:
            await self._finish_render(render, final_text or "(no reply)", include_thread=True)
        else:
            await self._finish_render(render, render.text, include_thread=True)

    async def _handle_realm_message(self, message: AgentMessage) -> None:
        chat_id = await self.store.chat_for_thread(message.thread_id)
        if chat_id is None:
            return
        if await self._handle_render_event(chat_id, message):
            return
        text = extract_text(message.payload)
        if not text:
            return
        sender = message.from_agent or message.from_account_id or "realm"
        await self.telegram.send_message(chat_id, f"{sender}: {text}")

    async def _start_render(self, chat_id: int, target: str, thread_id: str) -> RenderState:
        message_id = await self.telegram.send_message(chat_id, f"{target} is working...")
        render = RenderState(chat_id=chat_id, target=target, thread_id=thread_id, message_id=message_id)
        self._renders_by_thread[thread_id] = render
        return render

    async def _handle_render_event(self, chat_id: int, message: AgentMessage) -> bool:
        event_type = payload_type(message.payload)
        render = self._renders_by_thread.get(str(message.thread_id or ""))
        if event_type == "progress":
            text = progress_text(message.payload)
            if render is not None and text:
                await self._update_render(render, text)
            return True
        stream = stream_text(message.payload)
        if stream is None:
            return False
        stream_event, text = stream
        if render is None:
            target = message.from_agent or message.from_account_id or "realm"
            render = RenderState(chat_id=chat_id, target=target, thread_id=str(message.thread_id or ""))
            self._renders_by_thread[render.thread_id] = render
        if stream_event == "delta" and text:
            render.seq_text.append(text)
            await self._update_render(render, "".join(render.seq_text))
        elif stream_event == "end":
            await self._finish_render(render, text or render.text, include_thread=True)
        elif stream_event == "error":
            await self._finish_render(render, text or "stream failed")
        return True

    async def _update_render(self, render: RenderState, text: str) -> None:
        render.text = text
        now = time.monotonic()
        if now - render.last_edit_at < TELEGRAM_EDIT_INTERVAL_SECONDS:
            return
        render.last_edit_at = now
        await self._write_render(render, include_thread=False, in_progress=True)

    async def _finish_render(self, render: RenderState, text: str, *, include_thread: bool = False) -> None:
        render.text = text
        await self._write_render(render, include_thread=include_thread, in_progress=False)
        self._renders_by_thread.pop(render.thread_id, None)

    async def _write_render(self, render: RenderState, *, include_thread: bool, in_progress: bool) -> None:
        text = render_text(
            render.target,
            render.text,
            thread_id=render.thread_id if include_thread else None,
            in_progress=in_progress,
        )
        if render.message_id is None:
            render.message_id = await self.telegram.send_message(render.chat_id, text)
            return
        try:
            await self.telegram.edit_message_text(render.chat_id, render.message_id, text)
        except Exception:
            LOGGER.exception("Telegram render edit failed")
            render.message_id = await self.telegram.send_message(render.chat_id, text)


def _parse_allowed_chat_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    ids: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        ids.add(int(item))
    return ids


async def _run_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = args.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    gateway = TelegramRealmGateway(
        token=token,
        nats_url=args.nats_url,
        agent_id=args.agent_id,
        username=args.username,
        name=args.name,
        state_path=args.state_path,
        request_timeout=args.request_timeout,
        allowed_chat_ids=_parse_allowed_chat_ids(args.allowed_chat_ids),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, gateway.stop)
        except NotImplementedError:
            pass
    await gateway.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run a Telegram-to-Realm gateway.")
    parser.add_argument("--telegram-token", help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--nats-url", default=os.getenv("REALM_NATS_URL", DEFAULT_NATS_URL))
    parser.add_argument("--agent-id", default=os.getenv("REALM_GATEWAY_AGENT_ID", "telegram-gateway"))
    parser.add_argument("--username", default=os.getenv("REALM_GATEWAY_USERNAME", "telegram-gateway"))
    parser.add_argument("--name", default=os.getenv("REALM_GATEWAY_NAME", "Telegram Gateway"))
    parser.add_argument(
        "--state-path",
        default=os.getenv(
            "REALM_GATEWAY_STATE",
            os.path.expanduser("~/.local/share/realm/telegram-gateway.json"),
        ),
    )
    parser.add_argument(
        "--allowed-chat-ids",
        default=os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        help="Comma-separated Telegram chat IDs allowed to use the gateway. Empty allows all.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(os.getenv("REALM_GATEWAY_REQUEST_TIMEOUT", "86400")),
    )
    parser.add_argument("--debug", action="store_true", default=env_bool("REALM_GATEWAY_DEBUG"))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
