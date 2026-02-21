import asyncio

from agentnet import AgentNode


async def main() -> None:
    node = AgentNode(
        agent_id="agent_b",
        name="Agent B",
        capabilities=["receive", "notify"],
        nats_url="nats://localhost:4222",
    )

    @node.on_message
    async def on_message(msg):
        print(f"[agent_b] from={msg.from_agent} payload={msg.payload}")

    print("agent_b online; waiting for messages")
    await node.start_forever()


if __name__ == "__main__":
    asyncio.run(main())
