import asyncio
import os

from agentnet import AgentNode
from agentnet.config import DEFAULT_NATS_URL

from novita import DEFAULT_NOVITA_MODEL, novita_chat
from pretty_logs import print_event, print_header


async def build_first_question(goal: str, model: str, temperature: float, max_tokens: int) -> str:
    return await novita_chat(
        model=model,
        system_prompt=(
            "You are Agent A. Produce one plain-text technical question under 30 words. "
            "No markdown, no bullets, no preamble."
        ),
        user_prompt=f"Goal: {goal}",
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def build_followup_question(goal: str, previous_answer: str, model: str, temperature: float, max_tokens: int) -> str:
    return await novita_chat(
        model=model,
        system_prompt=(
            "You are Agent A. Ask one plain-text follow-up question under 30 words "
            "that deepens the previous answer. No markdown, no bullets."
        ),
        user_prompt=f"Goal: {goal}\nPrevious answer: {previous_answer}",
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def main() -> None:
    model = os.getenv("NOVITA_MODEL", DEFAULT_NOVITA_MODEL)
    temperature = float(os.getenv("NOVITA_TEMPERATURE", "0.7"))
    max_tokens = int(os.getenv("NOVITA_MAX_TOKENS", "256"))
    log_max_chars = int(os.getenv("LOG_TEXT_MAX_CHARS", "700"))
    request_timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
    turns = max(1, int(os.getenv("DEMO_TURNS", "4")))
    goal = os.getenv("DEMO_GOAL", "Design a robust architecture for an AI weather assistant.")
    target_username = os.getenv("TARGET_USERNAME", "agent_novita_b").lstrip("@")

    node = AgentNode(
        agent_id=os.getenv("AGENT_A_ID", "agent_novita_a"),
        name="Novita Agent A",
        username=os.getenv("AGENT_A_USERNAME", "agent_novita_a"),
        nats_url=os.getenv("NATS_URL", DEFAULT_NATS_URL),
    )
    await node.start()

    try:
        print_header("AGENT A", agent_id=node.agent_id, session_tag=node.session_tag, model=model)
        question = (await build_first_question(goal, model, temperature, max_tokens)).strip()
        if not question:
            question = f"What is your best practical approach for: {goal}?"

        for turn in range(1, turns + 1):
            print_event(
                "AGENT A",
                "SEND",
                question,
                turn=turn,
                peer=f"@{target_username}",
                session_tag=node.session_tag,
                max_chars=log_max_chars,
            )
            reply = await node.request_username(
                username=target_username,
                payload={"text": question, "goal": goal, "turn": turn},
                timeout=request_timeout,
            )
            payload = reply.payload if isinstance(reply.payload, dict) else {"text": str(reply.payload)}
            answer = str(payload.get("text") or "").strip()
            if not answer:
                answer = "<empty response>"
            print_event(
                "AGENT A",
                "RECV",
                answer,
                turn=turn,
                peer=reply.from_agent,
                session_tag=reply.from_session_tag,
                trace_id=reply.trace_id,
                max_chars=log_max_chars,
            )

            if turn < turns:
                question = (await build_followup_question(goal, answer, model, temperature, max_tokens)).strip()
                if not question:
                    question = f"Can you provide one concrete example for: {goal}?"
    finally:
        await node.close()


if __name__ == "__main__":
    asyncio.run(main())
