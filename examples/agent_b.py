import asyncio

from agentnet import AgentNode
from agentnet.config import DEFAULT_NATS_URL


async def main() -> None:
    node = AgentNode(
        agent_id="agent_b",
        name="Agent B",
        username="agent_b",
        capabilities=["receive", "notify"],
        nats_url=DEFAULT_NATS_URL,
    )

    @node.on_message
    async def on_message(msg):
        print(f"[agent_b] from={msg.from_agent} session={msg.from_session_tag} payload={msg.payload}")

    print("agent_b online; waiting for messages")
    await node.start_forever()


if __name__ == "__main__":
    asyncio.run(main())
