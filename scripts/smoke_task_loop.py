#!/usr/bin/env python3
"""Offline smoke: task.assign → progress (ack/working/tool) → result.

No NATS, OpenCode, or registry process required.

  PYTHONPATH=src python3 scripts/smoke_task_loop.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentnet.registry import _agent_status_summary  # noqa: E402
from agentnet.task_protocol import (  # noqa: E402
    TASK_PROGRESS,
    TASK_RESULT,
    build_task_assign,
    build_task_progress,
    build_task_result,
    is_terminal_task_payload,
    parent_task_id_from_payload,
    task_id_from_payload,
    task_type,
)


def _load_registry_main():
    path = ROOT / "services" / "registry" / "main.py"
    spec = importlib.util.spec_from_file_location("realm_registry_main_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load registry main from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _message_row(
    payload: dict[str, Any],
    *,
    message_id: str,
    sent_at: str,
    from_account_id: str,
    to_account_id: str,
) -> dict[str, Any]:
    return {
        "payload": payload,
        "message_id": message_id,
        "sent_at": sent_at,
        "received_at": sent_at,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "thread_id": "thread_smoke",
        "parent_message_id": None,
        "to_agent": None,
    }


def _check(name: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{mark}] {name}{suffix}")
    return condition


def main() -> int:
    print("smoke_task_loop: offline agentic job loop")
    print("-" * 48)
    ok = True

    parent_id = "task_parent_smoke"
    child_id = "task_child_smoke"

    parent = build_task_assign(
        task_id=parent_id,
        text="Ship the API fix",
        coordinator="boss",
        title="api-fix",
    )
    child = build_task_assign(
        task_id=child_id,
        text="Implement the endpoint",
        coordinator="sandra",
        title="implement-endpoint",
        parent_task_id=parent_id,
    )
    ack = build_task_progress(task_id=child_id, text="ack: accepted", phase="ack")
    working = build_task_progress(
        task_id=child_id,
        text="working on handler",
        percent=40,
        phase="working",
    )
    tool = build_task_progress(
        task_id=child_id,
        text="tool:write_file tests/test_api.py",
        percent=70,
        phase="tool",
    )
    result = build_task_result(
        task_id=child_id,
        text="Done: endpoint + tests",
        status="completed",
    )

    print("1. task_protocol builders")
    ok &= _check("parent assign type", task_type(parent) == "task.assign")
    ok &= _check("child parent_task_id", parent_task_id_from_payload(child) == parent_id)
    ok &= _check("progress types", all(task_type(p) == TASK_PROGRESS for p in (ack, working, tool)))
    ok &= _check("progress phases", [ack, working, tool][0]["metadata"]["phase"] == "ack")
    ok &= _check("result terminal", is_terminal_task_payload(result) and task_type(result) == TASK_RESULT)
    ok &= _check("task_ids stable", task_id_from_payload(child) == child_id == task_id_from_payload(result))

    print("2. registry snapshot (importlib, no NATS)")
    try:
        mod = _load_registry_main()
    except Exception as exc:  # pragma: no cover
        print(f"  [FAIL] load registry main: {exc}")
        return 1

    rows = [
        _message_row(
            child,
            message_id="m1",
            sent_at="2026-07-09T10:00:00Z",
            from_account_id="acct_sandra",
            to_account_id="acct_daniela",
        ),
        _message_row(
            ack,
            message_id="m2",
            sent_at="2026-07-09T10:01:00Z",
            from_account_id="acct_daniela",
            to_account_id="acct_sandra",
        ),
        _message_row(
            working,
            message_id="m3",
            sent_at="2026-07-09T10:02:00Z",
            from_account_id="acct_daniela",
            to_account_id="acct_sandra",
        ),
        _message_row(
            tool,
            message_id="m4",
            sent_at="2026-07-09T10:03:00Z",
            from_account_id="acct_daniela",
            to_account_id="acct_sandra",
        ),
        _message_row(
            result,
            message_id="m5",
            sent_at="2026-07-09T10:04:00Z",
            from_account_id="acct_daniela",
            to_account_id="acct_sandra",
        ),
    ]
    events = [mod._task_event_from_message_row(row) for row in rows]
    if any(e is None for e in events):
        print("  [FAIL] _task_event_from_message_row returned None")
        return 1

    mid = mod._task_snapshot_from_events(events[:-1])
    final = mod._task_snapshot_from_events(events)

    ok &= _check("mid status working", mid is not None and mid.get("status") == "working")
    ok &= _check(
        "mid latest_progress_text",
        mid is not None and mid.get("latest_progress_text") == "tool:write_file tests/test_api.py",
    )
    ok &= _check("mid parent_task_id", mid is not None and mid.get("parent_task_id") == parent_id)
    ok &= _check("mid not terminal", mid is not None and mid.get("terminal") is False)
    ok &= _check("final status completed", final is not None and final.get("status") == "completed")
    ok &= _check("final terminal", final is not None and final.get("terminal") is True)
    ok &= _check(
        "final keeps parent_task_id",
        final is not None and final.get("parent_task_id") == parent_id,
    )
    ok &= _check(
        "final keeps latest_progress_text",
        final is not None and final.get("latest_progress_text") == "tool:write_file tests/test_api.py",
    )
    hist = (final or {}).get("progress_history") or []
    ok &= _check("progress_history len", len(hist) == 3, detail=str(len(hist)))
    ok &= _check(
        "progress_history phases",
        [h.get("phase") for h in hist] == ["ack", "working", "tool"],
        detail=str([h.get("phase") for h in hist]),
    )
    ev = (final or {}).get("event_history") or []
    ok &= _check("event_history has assign+progress+result", len(ev) == 5, detail=str(len(ev)))

    print("3. agent_status summary")
    offline = _agent_status_summary(label="@daniela", online=False, active_tasks=[])
    active = _agent_status_summary(
        label="@daniela",
        online=True,
        active_tasks=[
            {
                "status": "working",
                "title": "implement-endpoint",
                "latest_progress_text": "tool:write_file tests/test_api.py",
            }
        ],
    )
    ok &= _check("offline summary", offline == "@daniela is offline")
    ok &= _check(
        "online+active summary",
        active == "@daniela is working on implement-endpoint: tool:write_file tests/test_api.py",
        detail=active,
    )

    print("-" * 48)
    if ok:
        print("PASS: offline task loop OK")
        return 0
    print("FAIL: offline task loop checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
