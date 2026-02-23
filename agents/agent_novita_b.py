import asyncio
import os
import signal
from typing import Any

from agentnet import AgentNode
from agentnet.config import DEFAULT_NATS_URL
from agentnet.schema import AgentMessage

from novita import DEFAULT_NOVITA_MODEL, novita_chat
from pretty_logs import print_event, print_header

async def main() -> None:
    model = os.getenv("NOVITA_MODEL", DEFAULT_NOVITA_MODEL)
    temperature = float(os.getenv("NOVITA_TEMPERATURE", "0.7"))
    max_tokens = int(os.getenv("NOVITA_MAX_TOKENS", "256"))
    log_max_chars = int(os.getenv("LOG_TEXT_MAX_CHARS", "700"))
    reply_ttl_ms = int(os.getenv("REPLY_TTL_MS", "30000"))
    system_prompt = os.getenv(
        "AGENT_B_SYSTEM_PROMPT",
        (
            "You are Agent B. Give practical and technically correct replies in plain text. "
            "Use short paragraphs, no markdown tables, no code fences. Keep answers under 120 words."
        ),
    )
    node = AgentNode(
        agent_id=os.getenv("AGENT_B_ID", "agent_novita_b"),
        name="Novita Agent B",
        username=os.getenv("AGENT_B_USERNAME", "agent_novita_b"),
        capabilities=["novita.chat"],
        nats_url=os.getenv("NATS_URL", DEFAULT_NATS_URL),
    )

    @node.on_message
    async def on_message(msg: AgentMessage) -> None:
        payload: dict[str, Any]
        if isinstance(msg.payload, dict):
            payload = msg.payload
        else:
            payload = {"text": str(msg.payload)}

        user_text = str(payload.get("text") or "").strip()
        if not user_text:
            user_text = "No question was provided."
        turn_raw = payload.get("turn")
        try:
            turn = int(turn_raw) if turn_raw is not None else None
        except (TypeError, ValueError):
            turn = None

        print_event(
            "AGENT B",
            "RECV",
            user_text,
            turn=turn,
            peer=msg.from_agent,
            session_tag=msg.from_session_tag,
            trace_id=msg.trace_id or msg.message_id,
            max_chars=log_max_chars,
        )

        answer = await novita_chat(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_text,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        if msg.reply_to:
            reply_payload = {
                "text": answer,
                "model": model,
                "answered_by": node.agent_id,
            }
            reply_id = await node.reply(
                request=msg,
                payload=reply_payload,
                kind="reply",
                ttl_ms=reply_ttl_ms,
                trace_id=msg.trace_id or msg.message_id,
            )
            print_event(
                "AGENT B",
                "SEND",
                answer,
                turn=turn,
                peer=msg.from_agent,
                session_tag=node.session_tag,
                trace_id=msg.trace_id or reply_id,
                max_chars=log_max_chars,
            )

    await node.start()
    print_header("AGENT B", agent_id=node.agent_id, session_tag=node.session_tag, model=model)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await node.close()


if __name__ == "__main__":
    asyncio.run(main())
