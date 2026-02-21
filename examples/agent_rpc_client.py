import asyncio
import os

from agentnet import AgentNode


async def main() -> None:
    # Initialize the client node
    node = AgentNode(
        agent_id="agent_client",
        name="Client Node",
        nats_url=os.getenv("NATS_URL", "nats://localhost:4222"),
    )
    
    await node.start()
    print(f"[{node.agent_id}] Started")

    print("\n--- Testing Capability Routing ---")
    payload = {"action": "add", "x": 5, "y": 7}
    print(f"[{node.agent_id}] Requesting capability 'calculator' to add 5 + 7...")
    
    try:
        # Notice we don't specify *which* agent should do it, only the capability 'calculator'
        reply = await node.request_capability("calculator", payload, timeout=2.0)
        print(f"[{node.agent_id}] Received reply from {reply.from_agent}: {reply.payload}")
    except asyncio.TimeoutError:
        print(f"[{node.agent_id}] Request timed out. Is the calculator agent running?")

    await node.close()
    
if __name__ == "__main__":
    asyncio.run(main())
