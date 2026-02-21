import asyncio

from agentnet import list_online_agents
from agentnet.config import DEFAULT_NATS_URL


async def main() -> None:
    agents = await list_online_agents(DEFAULT_NATS_URL)
    for agent in agents:
        print(agent.to_dict())


if __name__ == "__main__":
    asyncio.run(main())
