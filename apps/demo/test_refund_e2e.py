#!/usr/bin/env python3
"""E2E: delegate two refund jobs to @refund_agent and print outcomes.

Requires @refund_agent online:
  PYTHONPATH=src python3 apps/demo/refund_agent.py

  PYTHONPATH=src python3 apps/demo/test_refund_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentnet.config import DEFAULT_NATS_URL
from agentnet.sdk import AgentSDK
from agentnet.task_protocol import task_id_from_payload, task_type

NATS_URL = os.getenv("REALM_NATS_URL") or os.getenv("NATS_URL") or DEFAULT_NATS_URL
TARGET = os.getenv("REFUND_AGENT", "@refund_agent")


async def main() -> int:
    # Pending task_id → Future[outcome]
    waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
    # Must register handler BEFORE start or inbox is not subscribed.
    sdk = AgentSDK(
        agent_id="refund_tester",
        name="Refund Tester",
        username="refund_tester",
        capabilities=["test", "coordinator"],
        nats_url=NATS_URL,
        metadata={"role": "other", "company_visible": False},
    )

    @sdk.receive
    async def on_message(msg: Any) -> None:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        t = task_type(payload)
        if not t:
            return
        tid = task_id_from_payload(payload)
        if not tid or tid not in waiters:
            return
        fut = waiters[tid]
        if fut.done():
            return
        if t == "task.progress":
            print(f"  [{tid[-8:]}] progress: {payload.get('text')}", flush=True)
            return
        if t in {"task.result", "task.blocked", "task.failed"}:
            print(f"  [{tid[-8:]}] TERMINAL {t}: {payload.get('text')}", flush=True)
            fut.set_result(
                {
                    "terminal_type": t,
                    "status": payload.get("status"),
                    "text": payload.get("text"),
                    "metadata": payload.get("metadata"),
                    "task_id": tid,
                }
            )

    await sdk.start()
    print(f"tester online → {TARGET} via {NATS_URL}", flush=True)

    online = await sdk.list_online()
    names = []
    for row in online or []:
        if isinstance(row, dict):
            names.append(str(row.get("username") or row.get("name") or ""))
        else:
            names.append(str(getattr(row, "username", "") or getattr(row, "name", "")))
    print(f"online usernames: {names}", flush=True)
    if "refund_agent" not in {n.lstrip("@") for n in names}:
        print("FAIL: @refund_agent not online", flush=True)
        await sdk.stop()
        return 2

    async def run_case(
        name: str,
        order_id: str,
        amount_cents: int,
        reason: str,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        result = await sdk.delegate_task(
            TARGET,
            f"Process refund order_id={order_id} amount_cents={amount_cents} reason={reason}",
            title="refund_requested",
            metadata={
                "order_id": order_id,
                "amount_cents": amount_cents,
                "reason": reason,
                "kind": "refunds",
            },
            require_delivery_ack=False,
        )
        task_id = None
        if isinstance(result.data, dict):
            task_id = result.data.get("task_id")
        task_id = str(task_id or result.trace_id or "")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        waiters[task_id] = fut
        print(f"[{name}] delegated task_id={task_id}", flush=True)
        try:
            outcome = await asyncio.wait_for(fut, timeout=timeout)
            outcome["case"] = name
            outcome["ok"] = True
            return outcome
        except asyncio.TimeoutError:
            print(f"  [{name}] TIMEOUT", flush=True)
            return {"case": name, "ok": False, "error": "timeout", "task_id": task_id}
        finally:
            waiters.pop(task_id, None)

    try:
        auto = await run_case("auto_under_limit", "ord_100", 2499, "item_damaged")
        await asyncio.sleep(0.3)
        human = await run_case("escalate_over_limit", "ord_200", 20000, "not_as_described")
    finally:
        await sdk.stop()

    print("\n=== SUMMARY ===", flush=True)
    print(auto, flush=True)
    print(human, flush=True)

    ok = True
    if auto.get("terminal_type") != "task.result" or auto.get("status") != "completed":
        print("FAIL: expected auto refund completed", flush=True)
        ok = False
    else:
        print("PASS: auto refund completed", flush=True)

    if human.get("status") != "blocked" and human.get("terminal_type") != "task.blocked":
        print("FAIL: expected over-limit to block/escalate", flush=True)
        ok = False
    else:
        print("PASS: over-limit blocked/escalated", flush=True)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
