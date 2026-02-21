import asyncio

from agentnet import AgentNode


async def main() -> None:
    node = AgentNode(
        agent_id="agent_a",
        name="Agent A",
        capabilities=["chat"],
        nats_url="nats://localhost:4222",
    )

    @node.on_message
    async def on_message(msg):
        print(f"[agent_a] from={msg.from_agent} payload={msg.payload}")

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
