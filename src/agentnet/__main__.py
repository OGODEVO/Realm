"""CLI for AgentNet."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agentnet.registry import list_online_agents
from agentnet.node import AgentNode


async def _run_list(nats_url: str, timeout: float) -> int:
    agents = await list_online_agents(nats_url=nats_url, timeout=timeout)
    print(json.dumps([agent.to_dict() for agent in agents], indent=2))
    return 0


async def _run_send(nats_url: str, to: str, to_capability: str | None, payload: str) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        print("Error: payload must be valid JSON", file=sys.stderr)
        return 1

    async with AgentNode(agent_id="cli_sender", name="CLI Sender", nats_url=nats_url) as node:
        if to_capability:
            msg_id = await node.send_to_capability(to_capability, data)
            print(f"Sent message {msg_id} to capability '{to_capability}'")
        else:
            msg_id = await node.send(to, data)
            print(f"Sent message {msg_id} to agent '{to}'")
    return 0


async def _run_request(nats_url: str, to: str, to_capability: str | None, payload: str, timeout: float) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        print("Error: payload must be valid JSON", file=sys.stderr)
        return 1

    async with AgentNode(agent_id="cli_requester", name="CLI Requester", nats_url=nats_url) as node:
        try:
            if to_capability:
                print(f"Requesting capability '{to_capability}'...")
                reply = await node.request_capability(to_capability, data, timeout=timeout)
            else:
                print(f"Requesting agent '{to}'...")
                reply = await node.request(to, data, timeout=timeout)
                
            print(json.dumps(reply.to_dict(), indent=2))
        except asyncio.TimeoutError:
            print("Error: Request timed out (no reply received)", file=sys.stderr)
            return 1
            
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentnet", description="AgentNet CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # List command
    list_parser = subparsers.add_parser("list", help="List currently online agents")
    list_parser.add_argument("--nats-url", default="nats://localhost:4222")
    list_parser.add_argument("--timeout", type=float, default=2.0)

    # Send command
    send_parser = subparsers.add_parser("send", help="Send a fire-and-forget message")
    send_parser.add_argument("--nats-url", default="nats://localhost:4222")
    group = send_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--to", help="Agent ID to send to")
    group.add_argument("--to-capability", help="Capability to send to")
    send_parser.add_argument("payload", help="JSON payload string")

    # Request command
    req_parser = subparsers.add_parser("request", help="Send an RPC request and await a reply")
    req_parser.add_argument("--nats-url", default="nats://localhost:4222")
    req_parser.add_argument("--timeout", type=float, default=5.0)
    group2 = req_parser.add_mutually_exclusive_group(required=True)
    group2.add_argument("--to", help="Agent ID to request")
    group2.add_argument("--to-capability", help="Capability to request")
    req_parser.add_argument("payload", help="JSON payload string")

    args = parser.parse_args()

    try:
        if args.command == "list":
            return asyncio.run(_run_list(nats_url=args.nats_url, timeout=args.timeout))
        elif args.command == "send":
            return asyncio.run(_run_send(args.nats_url, args.to, args.to_capability, args.payload))
        elif args.command == "request":
            return asyncio.run(_run_request(args.nats_url, args.to, args.to_capability, args.payload, args.timeout))
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
