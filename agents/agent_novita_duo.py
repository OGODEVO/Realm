import asyncio
import os
import signal
import time
from statistics import mean
from typing import Any

from agentnet import AgentNode
from agentnet.config import DEFAULT_NATS_URL
from agentnet.schema import AgentMessage

from novita import DEFAULT_NOVITA_MODEL, novita_chat
from pretty_logs import print_event, print_header, print_metrics


def _build_node(
    *,
    agent_id: str,
    username: str,
    name: str,
    nats_url: str,
) -> AgentNode:
    return AgentNode(
        agent_id=agent_id,
        name=name,
        username=username,
        capabilities=["novita.chat"],
        nats_url=nats_url,
    )


async def _wire_responder(
    *,
    role: str,
    node: AgentNode,
    stats: dict[str, Any],
    model: str,
    temperature: float,
    max_tokens: int,
    log_max_chars: int,
    reply_ttl_ms: int,
    system_prompt: str,
) -> None:
    @node.on_message
    async def on_message(msg: AgentMessage) -> None:
        handle_started = time.perf_counter()
        payload: dict[str, Any]
        if isinstance(msg.payload, dict):
            payload = msg.payload
        else:
            payload = {"text": str(msg.payload)}

        user_text = str(payload.get("text") or "").strip() or "No question was provided."
        turn_raw = payload.get("turn")
        try:
            turn = int(turn_raw) if turn_raw is not None else None
        except (TypeError, ValueError):
            turn = None

        print_event(
            role,
            "RECV",
            user_text,
            turn=turn,
            peer=msg.from_agent,
            session_tag=msg.from_session_tag,
            trace_id=msg.trace_id or msg.message_id,
            thread_id=msg.thread_id,
            message_id=msg.message_id,
            parent_message_id=msg.parent_message_id,
            from_account_id=msg.from_account_id,
            to_account_id=msg.to_account_id,
            status=msg.kind,
            max_chars=log_max_chars,
        )
        stats["received"] += 1

        if not msg.reply_to:
            handle_ms = (time.perf_counter() - handle_started) * 1000.0
            stats["handle_ms"].append(handle_ms)
            print_event(
                role,
                "NO_REPLY",
                "Inbound message had no reply subject.",
                turn=turn,
                peer=msg.from_agent,
                session_tag=node.session_tag,
                trace_id=msg.trace_id or msg.message_id,
                thread_id=msg.thread_id,
                message_id=msg.message_id,
                handle_ms=handle_ms,
                status="accepted",
                max_chars=log_max_chars,
            )
            return

        llm_started = time.perf_counter()
        answer = await novita_chat(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_text,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        llm_ms = (time.perf_counter() - llm_started) * 1000.0
        reply_payload = {
            "text": answer,
            "model": model,
            "answered_by": node.agent_id,
            "answered_by_username": node.username,
        }
        send_started = time.perf_counter()
        reply_id = await node.reply(
            request=msg,
            payload=reply_payload,
            kind="reply",
            ttl_ms=reply_ttl_ms,
            trace_id=msg.trace_id or msg.message_id,
        )
        send_ms = (time.perf_counter() - send_started) * 1000.0
        handle_ms = (time.perf_counter() - handle_started) * 1000.0
        stats["replied"] += 1
        stats["handle_ms"].append(handle_ms)
        stats["llm_ms"].append(llm_ms)
        stats["send_ms"].append(send_ms)
        print_event(
            role,
            "SEND",
            answer,
            turn=turn,
            peer=msg.from_agent,
            session_tag=node.session_tag,
            trace_id=msg.trace_id or reply_id,
            thread_id=msg.thread_id,
            message_id=reply_id,
            parent_message_id=msg.message_id,
            from_account_id=node.account_id,
            to_account_id=msg.from_account_id,
            status="reply_sent",
            handle_ms=handle_ms,
            extra={"llm_ms": llm_ms, "reply_send_ms": send_ms},
            max_chars=log_max_chars,
        )
        print_metrics(role, "METRICS", _selected_metrics(node), turn=turn)


def _selected_metrics(node: AgentNode) -> dict[str, float | int | str | bool | None]:
    raw = node.metrics_snapshot()
    keys = (
        "inflight_count",
        "pending_count",
        "processed_count",
        "dropped_count",
        "timeout_count",
        "rate_limited_count",
        "duplicate_count",
        "busy_count",
        "avg_handle_ms",
        "circuit_open",
    )
    return {key: raw.get(key) for key in keys}


async def main() -> None:
    model = os.getenv("NOVITA_MODEL", DEFAULT_NOVITA_MODEL)
    temperature = float(os.getenv("NOVITA_TEMPERATURE", "0.7"))
    max_tokens = int(os.getenv("NOVITA_MAX_TOKENS", "256"))
    log_max_chars = int(os.getenv("LOG_TEXT_MAX_CHARS", "700"))
    reply_ttl_ms = int(os.getenv("REPLY_TTL_MS", "30000"))
    nats_url = os.getenv("NATS_URL", DEFAULT_NATS_URL)

    node_b = _build_node(
        agent_id=os.getenv("AGENT_B_ID", "agent_novita_b"),
        username=os.getenv("AGENT_B_USERNAME", "agent_novita_b"),
        name="Novita Agent B",
        nats_url=nats_url,
    )
    node_c = _build_node(
        agent_id=os.getenv("AGENT_C_ID", "agent_novita_c"),
        username=os.getenv("AGENT_C_USERNAME", "agent_novita_c"),
        name="Novita Agent C",
        nats_url=nats_url,
    )

    system_prompt_b = os.getenv(
        "AGENT_B_SYSTEM_PROMPT",
        (
            "You are Agent B. Give practical and technically correct replies in plain text. "
            "Use short paragraphs, no markdown tables, no code fences. Keep answers under 120 words."
        ),
    )
    system_prompt_c = os.getenv(
        "AGENT_C_SYSTEM_PROMPT",
        (
            "You are Agent C. Give concise, opinionated architecture feedback in plain text. "
            "Use short paragraphs, no markdown tables, no code fences. Keep answers under 120 words."
        ),
    )

    stats_b = {"received": 0, "replied": 0, "handle_ms": [], "llm_ms": [], "send_ms": []}
    stats_c = {"received": 0, "replied": 0, "handle_ms": [], "llm_ms": [], "send_ms": []}
    await _wire_responder(
        role="AGENT B",
        node=node_b,
        stats=stats_b,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        log_max_chars=log_max_chars,
        reply_ttl_ms=reply_ttl_ms,
        system_prompt=system_prompt_b,
    )
    await _wire_responder(
        role="AGENT C",
        node=node_c,
        stats=stats_c,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        log_max_chars=log_max_chars,
        reply_ttl_ms=reply_ttl_ms,
        system_prompt=system_prompt_c,
    )

    await node_b.start()
    await node_c.start()
    print_header("AGENT B", agent_id=node_b.agent_id, session_tag=node_b.session_tag, model=model)
    print_header("AGENT C", agent_id=node_c.agent_id, session_tag=node_c.session_tag, model=model)
    print_metrics("AGENT B", "METRICS_START", _selected_metrics(node_b))
    print_metrics("AGENT C", "METRICS_START", _selected_metrics(node_c))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    print_metrics(
        "AGENT B",
        "SUMMARY",
        {
            "received": stats_b["received"],
            "replied": stats_b["replied"],
            "handle_avg_ms": mean(stats_b["handle_ms"]) if stats_b["handle_ms"] else 0.0,
            "llm_avg_ms": mean(stats_b["llm_ms"]) if stats_b["llm_ms"] else 0.0,
            "reply_send_avg_ms": mean(stats_b["send_ms"]) if stats_b["send_ms"] else 0.0,
        },
    )
    print_metrics(
        "AGENT C",
        "SUMMARY",
        {
            "received": stats_c["received"],
            "replied": stats_c["replied"],
            "handle_avg_ms": mean(stats_c["handle_ms"]) if stats_c["handle_ms"] else 0.0,
            "llm_avg_ms": mean(stats_c["llm_ms"]) if stats_c["llm_ms"] else 0.0,
            "reply_send_avg_ms": mean(stats_c["send_ms"]) if stats_c["send_ms"] else 0.0,
        },
    )
    await node_b.close()
    await node_c.close()


if __name__ == "__main__":
    asyncio.run(main())
