import asyncio
import os
import time
from statistics import mean

from agentnet import AgentNode
from agentnet.config import DEFAULT_NATS_URL
from agentnet.schema import AgentMessage
from agentnet.utils import new_ulid

from novita import DEFAULT_NOVITA_MODEL, novita_chat
from pretty_logs import print_event, print_header, print_metrics


async def build_question(goal: str, model: str, temperature: float, max_tokens: int) -> str:
    return await novita_chat(
        model=model,
        system_prompt=(
            "You are Agent A coordinator. Produce one plain-text architecture question under 30 words. "
            "No markdown, no bullets."
        ),
        user_prompt=f"Goal: {goal}",
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def compare_answers(
    *,
    question: str,
    answer_b: str,
    answer_c: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    return await novita_chat(
        model=model,
        system_prompt=(
            "You are Agent A coordinator. Compare two agent answers and return exactly 2 short lines:\n"
            "Line 1: strongest point from B.\n"
            "Line 2: strongest point from C.\n"
            "No markdown."
        ),
        user_prompt=f"Question: {question}\nAnswer B: {answer_b}\nAnswer C: {answer_c}",
        temperature=temperature,
        max_tokens=max_tokens,
    )


def extract_text(reply: AgentMessage) -> str:
    payload = reply.payload if isinstance(reply.payload, dict) else {"text": str(reply.payload)}
    return str(payload.get("text") or "").strip() or "<empty response>"


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
        "retry_count",
        "receipt_timeout_count",
        "avg_handle_ms",
        "circuit_open",
    )
    return {key: raw.get(key) for key in keys}


async def _timed_request(
    node: AgentNode,
    *,
    username: str,
    payload: dict[str, object],
    timeout: float,
    thread_id: str,
) -> tuple[AgentMessage, float]:
    started = time.perf_counter()
    reply = await node.request_username(
        username=username,
        payload=payload,
        timeout=timeout,
        thread_id=thread_id,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return reply, elapsed_ms


async def _timed_send_feedback(
    node: AgentNode,
    *,
    username: str,
    payload: dict[str, object],
    thread_id: str,
    parent_message_id: str | None,
) -> tuple[str, float]:
    started = time.perf_counter()
    msg_id = await node.send_to_username(
        username,
        payload,
        thread_id=thread_id,
        parent_message_id=parent_message_id,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return msg_id, elapsed_ms


async def main() -> None:
    model = os.getenv("NOVITA_MODEL", DEFAULT_NOVITA_MODEL)
    temperature = float(os.getenv("NOVITA_TEMPERATURE", "0.7"))
    max_tokens = int(os.getenv("NOVITA_MAX_TOKENS", "256"))
    log_max_chars = int(os.getenv("LOG_TEXT_MAX_CHARS", "700"))
    request_timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
    turns = max(1, int(os.getenv("DEMO_TURNS", "2")))
    goal = os.getenv("DEMO_GOAL", "Design a robust architecture for an AI weather assistant.")
    target_b = os.getenv("TARGET_B_USERNAME", os.getenv("AGENT_B_USERNAME", "agent_novita_b")).lstrip("@")
    target_c = os.getenv("TARGET_C_USERNAME", os.getenv("AGENT_C_USERNAME", "agent_novita_c")).lstrip("@")
    thread_id = os.getenv("DEMO_THREAD_ID", f"demo_{new_ulid().lower()}")

    node = AgentNode(
        agent_id=os.getenv("AGENT_A_ID", "agent_novita_a"),
        name="Novita Agent A Coordinator",
        username=os.getenv("AGENT_A_USERNAME", "agent_novita_a"),
        nats_url=os.getenv("NATS_URL", DEFAULT_NATS_URL),
    )
    await node.start()
    request_latencies_ms: list[float] = []
    send_ack_latencies_ms: list[float] = []
    compare_latencies_ms: list[float] = []
    success_replies = 0
    send_failures = 0

    try:
        print_header("AGENT A", agent_id=node.agent_id, session_tag=node.session_tag, model=model)
        print_metrics("AGENT A", "METRICS_START", _selected_metrics(node))

        for turn in range(1, turns + 1):
            question = (await build_question(goal, model, temperature, max_tokens)).strip()
            if not question:
                question = f"What is your best practical approach for: {goal}?"

            print_event(
                "AGENT A",
                "ASK",
                question,
                turn=turn,
                peer=f"@{target_b}, @{target_c}",
                session_tag=node.session_tag,
                thread_id=thread_id,
                status="request_start",
                max_chars=log_max_chars,
            )

            reply_b, reply_c = await asyncio.gather(
                _timed_request(
                    node,
                    username=target_b,
                    payload={"text": question, "goal": goal, "turn": turn, "to": target_b},
                    timeout=request_timeout,
                    thread_id=thread_id,
                ),
                _timed_request(
                    node,
                    username=target_c,
                    payload={"text": question, "goal": goal, "turn": turn, "to": target_c},
                    timeout=request_timeout,
                    thread_id=thread_id,
                ),
            )
            reply_b_msg, reply_b_ms = reply_b
            reply_c_msg, reply_c_ms = reply_c
            request_latencies_ms.extend([reply_b_ms, reply_c_ms])
            success_replies += 2
            answer_b = extract_text(reply_b_msg)
            answer_c = extract_text(reply_c_msg)

            print_event(
                "AGENT A",
                "RECV_B",
                answer_b,
                turn=turn,
                peer=reply_b_msg.from_agent,
                session_tag=reply_b_msg.from_session_tag,
                trace_id=reply_b_msg.trace_id,
                thread_id=reply_b_msg.thread_id,
                message_id=reply_b_msg.message_id,
                parent_message_id=reply_b_msg.parent_message_id,
                from_account_id=reply_b_msg.from_account_id,
                to_account_id=reply_b_msg.to_account_id,
                status=reply_b_msg.kind,
                latency_ms=reply_b_ms,
                max_chars=log_max_chars,
            )
            print_event(
                "AGENT A",
                "RECV_C",
                answer_c,
                turn=turn,
                peer=reply_c_msg.from_agent,
                session_tag=reply_c_msg.from_session_tag,
                trace_id=reply_c_msg.trace_id,
                thread_id=reply_c_msg.thread_id,
                message_id=reply_c_msg.message_id,
                parent_message_id=reply_c_msg.parent_message_id,
                from_account_id=reply_c_msg.from_account_id,
                to_account_id=reply_c_msg.to_account_id,
                status=reply_c_msg.kind,
                latency_ms=reply_c_ms,
                max_chars=log_max_chars,
            )

            compare_started = time.perf_counter()
            comparison = (await compare_answers(
                question=question,
                answer_b=answer_b,
                answer_c=answer_c,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )).strip() or "B and C gave complementary tradeoffs."
            compare_ms = (time.perf_counter() - compare_started) * 1000.0
            compare_latencies_ms.append(compare_ms)
            print_event(
                "AGENT A",
                "COMPARE",
                comparison,
                turn=turn,
                session_tag=node.session_tag,
                thread_id=thread_id,
                latency_ms=compare_ms,
                max_chars=log_max_chars,
            )

            note_to_b = f"Agent C said: {answer_c}\nCoordinator compare: {comparison}"
            note_to_c = f"Agent B said: {answer_b}\nCoordinator compare: {comparison}"

            send_results = await asyncio.gather(
                _timed_send_feedback(
                    node,
                    username=target_b,
                    payload={"text": note_to_b, "turn": turn, "kind": "peer_feedback"},
                    thread_id=thread_id,
                    parent_message_id=reply_b_msg.message_id,
                ),
                _timed_send_feedback(
                    node,
                    username=target_c,
                    payload={"text": note_to_c, "turn": turn, "kind": "peer_feedback"},
                    thread_id=thread_id,
                    parent_message_id=reply_c_msg.message_id,
                ),
                return_exceptions=True,
            )
            for idx, result in enumerate(send_results):
                target = target_b if idx == 0 else target_c
                parent = reply_b_msg.message_id if idx == 0 else reply_c_msg.message_id
                if isinstance(result, Exception):
                    send_failures += 1
                    print_event(
                        "AGENT A",
                        "SHARE_FAIL",
                        str(result),
                        turn=turn,
                        peer=f"@{target}",
                        session_tag=node.session_tag,
                        thread_id=thread_id,
                        parent_message_id=parent,
                        status="rejected",
                        max_chars=log_max_chars,
                    )
                else:
                    sent_message_id, send_ms = result
                    send_ack_latencies_ms.append(send_ms)
                    print_event(
                        "AGENT A",
                        "SHARE_OK",
                        "Feedback delivered and receipt acknowledged.",
                        turn=turn,
                        peer=f"@{target}",
                        session_tag=node.session_tag,
                        thread_id=thread_id,
                        message_id=sent_message_id,
                        parent_message_id=parent,
                        status="accepted",
                        latency_ms=send_ms,
                        max_chars=log_max_chars,
                    )

            print_metrics("AGENT A", "METRICS_TURN", _selected_metrics(node), turn=turn)
        request_avg = mean(request_latencies_ms) if request_latencies_ms else 0.0
        send_avg = mean(send_ack_latencies_ms) if send_ack_latencies_ms else 0.0
        compare_avg = mean(compare_latencies_ms) if compare_latencies_ms else 0.0
        print_metrics(
            "AGENT A",
            "SUMMARY",
            {
                "turns": turns,
                "thread": thread_id,
                "requests_ok": success_replies,
                "request_avg_ms": request_avg,
                "request_max_ms": max(request_latencies_ms) if request_latencies_ms else 0.0,
                "send_ok": len(send_ack_latencies_ms),
                "send_fail": send_failures,
                "send_ack_avg_ms": send_avg,
                "compare_avg_ms": compare_avg,
            },
        )
    finally:
        await node.close()


if __name__ == "__main__":
    asyncio.run(main())
