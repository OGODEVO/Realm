#!/usr/bin/env python3
"""Mini demo process: @refund_agent — tools only, no LLM.

Policy:
  - amount_cents <= AUTO_LIMIT → issue_refund
  - else → escalate (task.blocked)

  PYTHONPATH=src python3 apps/demo/refund_agent.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentnet.config import DEFAULT_NATS_URL
from agentnet.sdk import AgentSDK
from agentnet.task_protocol import (
    TASK_ASSIGN,
    build_task_result,
    task_id_from_payload,
    task_type,
)

AUTO_LIMIT_CENTS = int(os.getenv("REFUND_AUTO_LIMIT_CENTS", "5000"))  # $50
NATS_URL = os.getenv("REALM_NATS_URL") or os.getenv("NATS_URL") or DEFAULT_NATS_URL
USERNAME = os.getenv("REALM_USERNAME", "refund_agent")


# --- tools (only side effects) ------------------------------------------------

_ORDERS: dict[str, dict[str, Any]] = {
    "ord_100": {
        "order_id": "ord_100",
        "customer_id": "cus_1",
        "status": "paid",
        "amount_cents": 2499,
        "currency": "USD",
    },
    "ord_200": {
        "order_id": "ord_200",
        "customer_id": "cus_2",
        "status": "paid",
        "amount_cents": 20000,
        "currency": "USD",
    },
}
_REFUNDS: list[dict[str, Any]] = []
_ESCALATIONS: list[dict[str, Any]] = []


def get_order(order_id: str) -> dict[str, Any]:
    order = _ORDERS.get(order_id)
    if not order:
        return {"ok": False, "error": "order_not_found", "order_id": order_id}
    return {"ok": True, "order": dict(order)}


def issue_refund(order_id: str, amount_cents: int, reason: str = "") -> dict[str, Any]:
    order = _ORDERS.get(order_id)
    if not order:
        return {"ok": False, "error": "order_not_found", "order_id": order_id}
    refund_id = f"rf_{order_id}_{len(_REFUNDS) + 1}"
    row = {
        "ok": True,
        "refund_id": refund_id,
        "order_id": order_id,
        "amount_cents": int(amount_cents),
        "reason": reason,
        "status": "issued",
    }
    _REFUNDS.append(row)
    print(f"[tool:issue_refund] {row}", flush=True)
    return row


def escalate(reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "ok": True,
        "escalation_id": f"esc_{len(_ESCALATIONS) + 1}",
        "reason": reason,
        "evidence": evidence or {},
        "status": "queued_for_human",
    }
    _ESCALATIONS.append(row)
    print(f"[tool:escalate] {row}", flush=True)
    return row


def _extract_refund_fields(payload: dict[str, Any], text: str) -> dict[str, Any]:
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    order_id = str(meta.get("order_id") or "").strip()
    amount = meta.get("amount_cents")
    reason = str(meta.get("reason") or "unspecified").strip()

    # fallback: parse simple "order_id=... amount_cents=..." from text
    if not order_id or amount is None:
        for part in text.replace(",", " ").split():
            if part.startswith("order_id=") and not order_id:
                order_id = part.split("=", 1)[1].strip()
            if part.startswith("amount_cents=") and amount is None:
                try:
                    amount = int(part.split("=", 1)[1])
                except ValueError:
                    pass
            if part.startswith("reason=") and reason == "unspecified":
                reason = part.split("=", 1)[1].strip()

    try:
        amount_cents = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_cents = None

    return {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "reason": reason or "unspecified",
    }


async def handle_task(sdk: AgentSDK, msg: Any) -> None:
    payload = msg.payload if isinstance(msg.payload, dict) else {}
    to = msg.from_agent or ""
    # Prefer username/account on message if present
    if getattr(msg, "from_account_id", None):
        to = f"account:{msg.from_account_id}"
    # Coordinator field on task.assign
    coordinator = str(payload.get("coordinator") or "").strip()
    if coordinator:
        to = coordinator if coordinator.startswith("@") or coordinator.startswith("acct_") or coordinator.startswith("account:") else f"@{coordinator}"

    task_id = task_id_from_payload(payload) or str(getattr(msg, "trace_id", "") or "")
    text = str(payload.get("text") or "")
    fields = _extract_refund_fields(payload, text)
    thread_id = msg.thread_id

    async def progress(line: str, phase: str = "working", percent: int | None = None) -> None:
        if not to or not task_id:
            print(f"[progress] {line}", flush=True)
            return
        await sdk.report_progress(
            to,
            task_id,
            line,
            thread_id=thread_id,
            phase=phase,
            percent=percent,
            require_delivery_ack=False,
        )

    await progress(f"ACK: refund request {fields}", phase="ack", percent=5)

    order_id = fields["order_id"]
    amount_cents = fields["amount_cents"]
    reason = fields["reason"]

    if not order_id or amount_cents is None:
        result = build_task_result(
            task_id=task_id or "unknown",
            text="missing order_id or amount_cents",
            status="failed",
            metadata={"agent": USERNAME, "fields": fields},
        )
        if to:
            await sdk.send_json(to, result, thread_id=thread_id, require_delivery_ack=False)
        return

    await progress(f"tool: get_order({order_id})", phase="tool", percent=20)
    order_res = get_order(order_id)
    if not order_res.get("ok"):
        result = build_task_result(
            task_id=task_id,
            text=f"order not found: {order_id}",
            status="failed",
            metadata={"agent": USERNAME, "tool": order_res},
        )
        if to:
            await sdk.send_json(to, result, thread_id=thread_id, require_delivery_ack=False)
        return

    await progress(
        f"policy: amount={amount_cents} auto_limit={AUTO_LIMIT_CENTS}",
        phase="working",
        percent=40,
    )

    if amount_cents > AUTO_LIMIT_CENTS:
        await progress("tool: escalate(...)", phase="tool", percent=70)
        esc = escalate(
            reason=f"amount {amount_cents} over auto limit {AUTO_LIMIT_CENTS}",
            evidence={
                "order_id": order_id,
                "amount_cents": amount_cents,
                "reason": reason,
                "order": order_res.get("order"),
            },
        )
        blocked = build_task_result(
            task_id=task_id,
            text=f"needs human approval: {esc['reason']}",
            status="blocked",
            metadata={"agent": USERNAME, "escalation": esc},
        )
        if to:
            await sdk.send_json(
                to,
                blocked,
                thread_id=thread_id,
                idempotency_key=f"{task_id}:blocked",
                require_delivery_ack=False,
            )
        print(f"[terminal] blocked {task_id}", flush=True)
        return

    await progress(
        f"tool: issue_refund({order_id}, {amount_cents})",
        phase="tool",
        percent=80,
    )
    refund = issue_refund(order_id, amount_cents, reason=reason)
    result = build_task_result(
        task_id=task_id,
        text=f"refund issued {refund.get('refund_id')} for {order_id} amount={amount_cents}",
        status="completed",
        metadata={"agent": USERNAME, "refund": refund},
    )
    if to:
        await sdk.send_json(
            to,
            result,
            thread_id=thread_id,
            idempotency_key=f"{task_id}:result",
            require_delivery_ack=False,
        )
    print(f"[terminal] completed {task_id} {refund}", flush=True)


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    sdk = AgentSDK(
        agent_id=os.getenv("REALM_AGENT_ID", "refund_agent"),
        name=os.getenv("REALM_AGENT_NAME", "Refund Agent"),
        username=USERNAME,
        capabilities=["refunds", "demo"],
        nats_url=NATS_URL,
        metadata={
            "role": "worker",
            "company_visible": True,
            "kind": "demo-refund",
        },
        work_timeout_seconds=float(os.getenv("REALM_WORK_TIMEOUT_SECONDS", "120")),
    )

    @sdk.receive
    async def on_message(msg: Any) -> None:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if task_type(payload) != TASK_ASSIGN and not (
            isinstance(payload, dict) and str(payload.get("type") or "") == TASK_ASSIGN
        ):
            # still accept assign by type string
            if not (isinstance(payload, dict) and str(payload.get("type") or "").lower() == "task.assign"):
                print(f"[skip] non-task from {msg.from_agent}: {payload!r}"[:200], flush=True)
                return
        print(f"[task.assign] from={msg.from_agent} payload={payload}", flush=True)
        await handle_task(sdk, msg)

    await sdk.start()
    print(f"@{USERNAME.lstrip('@')} online nats={NATS_URL}", flush=True)
    print(f"auto_limit_cents={AUTO_LIMIT_CENTS}", flush=True)
    try:
        await stop.wait()
    finally:
        await sdk.stop()


if __name__ == "__main__":
    asyncio.run(main())
