import asyncio
import os
import signal
from typing import Any

from agentnet import AgentNode
from agentnet.schema import AgentMessage


async def main() -> None:
    # We initialize the node and advertise the 'calculator' capability.
    # It will automatically subscribe to 'agent.capability.calculator'.
    node = AgentNode(
        agent_id="agent_calculator_1",
        name="Calculator Node",
        capabilities=["calculator"],
        nats_url=os.getenv("NATS_URL", "nats://localhost:4222"),
    )

    @node.on_message
    async def handle_message(msg: AgentMessage) -> None:
        print(f"[{node.agent_id}] Received message from {msg.from_agent}: {msg.payload}")
        
        # If this message was sent as an RPC request, it will have a reply_to subject.
        if msg.reply_to and isinstance(msg.payload, dict) and msg.payload.get("action") == "add":
            x = msg.payload.get("x", 0)
            y = msg.payload.get("y", 0)
            result = x + y
            print(f"[{node.agent_id}] Calculating {x} + {y} = {result}. Sending reply.")
            
            # Note: We simulate sending the reply by using the underlying nats connection 
            # to publish to the reply_to subject inline. 
            import json
            reply_envelope = {
                "message_id": "reply_123",
                "from_agent": node.agent_id,
                "to_agent": msg.from_agent,
                "payload": {"result": result},
                "sent_at": "now",
                "kind": "reply"
            }
            await node._nc.publish(msg.reply_to, json.dumps(reply_envelope).encode("utf-8"))

    await node.start()
    print(f"[{node.agent_id}] Started with capabilities: {node.capabilities}")
    print("Waiting for calculations...")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await node.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
