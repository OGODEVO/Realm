import asyncio

from agentnet import AgentNode
from agentnet.config import DEFAULT_NATS_URL


async def main() -> None:
    node = AgentNode(
        agent_id="agent_a",
        name="Agent A",
        capabilities=["chat"],
        nats_url=DEFAULT_NATS_URL,
    )

    @node.on_message
    async def on_message(msg):
        print(f"[agent_a] from={msg.from_agent} session={msg.from_session_tag} payload={msg.payload}")

    await node.start()
    print("agent_a online; sending to agent_b every 8s")

    try:
        while True:
            await node.send("agent_b", {"text": "hello from agent_a"})
            await asyncio.sleep(8)
    finally:
        await node.close()


if __name__ == "__main__":
    asyncio.run(main())
