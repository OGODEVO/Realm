import asyncio

from agentnet import list_online_agents


async def main() -> None:
    agents = await list_online_agents("nats://localhost:4222")
    for agent in agents:
        print(agent.to_dict())


if __name__ == "__main__":
    asyncio.run(main())
