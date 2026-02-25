"""CLI for AgentNet."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agentnet.config import DEFAULT_NATS_URL
from agentnet.node import AgentNode
from agentnet.registry import get_profile, list_online_agents, search_profiles


async def _run_list(nats_url: str, timeout: float) -> int:
    agents = await list_online_agents(nats_url=nats_url, timeout=timeout)
    print(json.dumps([agent.to_dict() for agent in agents], indent=2))
    return 0


async def _run_search(
    nats_url: str,
    query: str,
    capability: str | None,
    limit: int,
    online_only: bool,
    timeout: float,
) -> int:
    results = await search_profiles(
        nats_url,
        query=query,
        capability=capability,
        limit=limit,
        online_only=online_only,
        timeout=timeout,
    )
    print(json.dumps(results, indent=2))
    return 0


async def _run_profile(
    nats_url: str,
    account_id: str | None,
    username: str | None,
    timeout: float,
) -> int:
    profile = await get_profile(
        nats_url,
        account_id=account_id,
        username=username,
        timeout=timeout,
    )
    print(json.dumps(profile, indent=2))
    return 0


async def _run_send(
    nats_url: str,
    to_account: str | None,
    to_username: str | None,
    to_capability: str | None,
    payload: str,
    thread_id: str | None,
    parent_message_id: str | None,
    retry_attempts: int,
    receipt_timeout: float,
    no_ack: bool,
) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        print("Error: payload must be valid JSON", file=sys.stderr)
        return 1

    async with AgentNode(agent_id="cli_sender", name="CLI Sender", nats_url=nats_url) as node:
        if to_capability:
            msg_id = await node.send_to_capability(
                to_capability,
                data,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                require_delivery_ack=not no_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
            print(f"Sent message {msg_id} to capability '{to_capability}'")
        elif to_account:
            msg_id = await node.send_to_account(
                to_account,
                data,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                require_delivery_ack=not no_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
            print(f"Sent message {msg_id} to account '{to_account}'")
        elif to_username:
            msg_id = await node.send_to_username(
                to_username,
                data,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                require_delivery_ack=not no_ack,
                retry_attempts=retry_attempts,
                receipt_timeout=receipt_timeout,
            )
            print(f"Sent message {msg_id} to username '{to_username}'")
        else:
            print("Error: one destination option is required", file=sys.stderr)
            return 1
    return 0


async def _run_request(
    nats_url: str,
    to_account: str | None,
    to_username: str | None,
    to_capability: str | None,
    payload: str,
    timeout: float,
    thread_id: str | None,
    parent_message_id: str | None,
) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        print("Error: payload must be valid JSON", file=sys.stderr)
        return 1

    async with AgentNode(agent_id="cli_requester", name="CLI Requester", nats_url=nats_url) as node:
        try:
            if to_capability:
                print(f"Requesting capability '{to_capability}'...")
                reply = await node.request_capability(
                    to_capability,
                    data,
                    timeout=timeout,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                )
            elif to_account:
                print(f"Requesting account '{to_account}'...")
                reply = await node.request_account(
                    to_account,
                    data,
                    timeout=timeout,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                )
            elif to_username:
                print(f"Requesting username '{to_username}'...")
                reply = await node.request_username(
                    to_username,
                    data,
                    timeout=timeout,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                )
            else:
                print("Error: one destination option is required", file=sys.stderr)
                return 1

            print(json.dumps(reply.to_dict(), indent=2))
        except asyncio.TimeoutError:
            print("Error: Request timed out (no reply received)", file=sys.stderr)
            return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentnet", description="AgentNet CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List currently online agents")
    list_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    list_parser.add_argument("--timeout", type=float, default=2.0)

    search_parser = subparsers.add_parser("search", help="Search agent profiles")
    search_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    search_parser.add_argument("--query", default="")
    search_parser.add_argument("--capability")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--online-only", action="store_true")
    search_parser.add_argument("--timeout", type=float, default=2.0)

    profile_parser = subparsers.add_parser("profile", help="Get account profile")
    profile_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    profile_group = profile_parser.add_mutually_exclusive_group(required=True)
    profile_group.add_argument("--account-id")
    profile_group.add_argument("--username")
    profile_parser.add_argument("--timeout", type=float, default=2.0)

    send_parser = subparsers.add_parser("send", help="Send a message")
    send_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    group = send_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--to-account", help="Account ID to send to")
    group.add_argument("--to-username", help="Username to send to")
    group.add_argument("--to-capability", help="Capability to send to")
    send_parser.add_argument("--thread-id", help="Thread ID to attach")
    send_parser.add_argument("--parent-message-id", help="Parent message ID")
    send_parser.add_argument("--retry-attempts", type=int, default=2, help="Retries after first publish")
    send_parser.add_argument("--receipt-timeout", type=float, default=1.5, help="Seconds to wait per attempt")
    send_parser.add_argument("--no-ack", action="store_true", help="Disable delivery-ack wait")
    send_parser.add_argument("payload", help="JSON payload string")

    req_parser = subparsers.add_parser("request", help="Send an RPC request and await a reply")
    req_parser.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    req_parser.add_argument("--timeout", type=float, default=5.0)
    req_group = req_parser.add_mutually_exclusive_group(required=True)
    req_group.add_argument("--to-account", help="Account ID to request")
    req_group.add_argument("--to-username", help="Username to request")
    req_group.add_argument("--to-capability", help="Capability to request")
    req_parser.add_argument("--thread-id", help="Thread ID to attach")
    req_parser.add_argument("--parent-message-id", help="Parent message ID")
    req_parser.add_argument("payload", help="JSON payload string")

    args = parser.parse_args()

    try:
        if args.command == "list":
            return asyncio.run(_run_list(nats_url=args.nats_url, timeout=args.timeout))
        if args.command == "search":
            return asyncio.run(
                _run_search(
                    nats_url=args.nats_url,
                    query=args.query,
                    capability=args.capability,
                    limit=args.limit,
                    online_only=args.online_only,
                    timeout=args.timeout,
                )
            )
        if args.command == "profile":
            return asyncio.run(
                _run_profile(
                    nats_url=args.nats_url,
                    account_id=args.account_id,
                    username=args.username,
                    timeout=args.timeout,
                )
            )
        if args.command == "send":
            return asyncio.run(
                _run_send(
                    args.nats_url,
                    args.to_account,
                    args.to_username,
                    args.to_capability,
                    args.payload,
                    args.thread_id,
                    args.parent_message_id,
                    args.retry_attempts,
                    args.receipt_timeout,
                    args.no_ack,
                )
            )
        if args.command == "request":
            return asyncio.run(
                _run_request(
                    args.nats_url,
                    args.to_account,
                    args.to_username,
                    args.to_capability,
                    args.payload,
                    args.timeout,
                    args.thread_id,
                    args.parent_message_id,
                )
            )
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
