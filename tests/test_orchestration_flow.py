"""Offline integration tests for the agentic job loop.

Proves the full chain without OpenCode or live NATS:

  task.assign → task.progress (ack/working/tool) → task.result
  parent_task_id child tasks
  _task_snapshot_from_events → latest_progress_text + parent_task_id
  _agent_status_summary helpers
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any

from agentnet.registry import _agent_status_summary
from agentnet.task_protocol import (
    TASK_ASSIGN,
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
    """Import registry pure helpers without requiring live services."""
    path = Path(__file__).resolve().parents[1] / "services" / "registry" / "main.py"
    spec = importlib.util.spec_from_file_location("realm_registry_main_orch", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - environment dependent
        raise unittest.SkipTest(f"registry main import failed: {exc}") from exc
    return mod


def _message_row(
    payload: dict[str, Any],
    *,
    message_id: str,
    sent_at: str,
    from_account_id: str,
    to_account_id: str,
    thread_id: str = "thread_orch",
) -> dict[str, Any]:
    return {
        "payload": payload,
        "message_id": message_id,
        "sent_at": sent_at,
        "received_at": sent_at,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "thread_id": thread_id,
        "parent_message_id": None,
        "to_agent": None,
    }


class TaskProtocolChainTests(unittest.TestCase):
    """Pure unit tests for task_protocol builders chain (parent, progress, result)."""

    def test_builders_chain_parent_progress_result(self) -> None:
        parent_id = "task_parent_orch"
        child_id = "task_child_orch"

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
        ack = build_task_progress(task_id=child_id, text="ack: got it", phase="ack")
        working = build_task_progress(
            task_id=child_id,
            text="coding handler",
            percent=30,
            phase="working",
        )
        tool = build_task_progress(
            task_id=child_id,
            text="tool:read_file src/api.py",
            percent=50,
            phase="tool",
        )
        result = build_task_result(
            task_id=child_id,
            text="Endpoint + tests ready",
            status="completed",
        )

        self.assertEqual(task_type(parent), TASK_ASSIGN)
        self.assertEqual(task_id_from_payload(parent), parent_id)
        self.assertEqual(parent_task_id_from_payload(parent), "")

        self.assertEqual(task_type(child), TASK_ASSIGN)
        self.assertEqual(task_id_from_payload(child), child_id)
        self.assertEqual(parent_task_id_from_payload(child), parent_id)
        self.assertEqual(child["parent_task_id"], parent_id)
        self.assertEqual(child["coordinator"], "sandra")

        for progress in (ack, working, tool):
            self.assertEqual(task_type(progress), TASK_PROGRESS)
            self.assertEqual(task_id_from_payload(progress), child_id)
            self.assertFalse(is_terminal_task_payload(progress))

        self.assertEqual(ack["metadata"]["phase"], "ack")
        self.assertEqual(working["metadata"]["phase"], "working")
        self.assertEqual(working["metadata"]["percent"], 30.0)
        self.assertEqual(tool["metadata"]["phase"], "tool")

        self.assertEqual(task_type(result), TASK_RESULT)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(is_terminal_task_payload(result))


class TaskSnapshotFlowTests(unittest.TestCase):
    """Registry snapshot reflects assign → progress → result, including parent link."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_registry_main()

    def _events_for_child_lifecycle(self) -> tuple[str, str, list[dict[str, Any]]]:
        parent_id = "task_parent_snap"
        child_id = "task_child_snap"
        assign = build_task_assign(
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

        rows = [
            _message_row(
                assign,
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
        events = []
        for row in rows:
            event = self.mod._task_event_from_message_row(row)
            self.assertIsNotNone(event, msg=f"failed to decode row {row['message_id']}")
            assert event is not None
            events.append(event)
        return parent_id, child_id, events

    def test_snapshot_working_after_progress(self) -> None:
        parent_id, child_id, events = self._events_for_child_lifecycle()
        # Up through last progress (exclude terminal result)
        progress_events = events[:-1]
        snapshot = self.mod._task_snapshot_from_events(progress_events)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None

        self.assertEqual(snapshot["task_id"], child_id)
        self.assertEqual(snapshot["status"], "working")
        self.assertEqual(snapshot["type"], TASK_PROGRESS)
        self.assertEqual(snapshot["parent_task_id"], parent_id)
        self.assertEqual(snapshot["coordinator"], "sandra")
        self.assertEqual(snapshot["latest_progress_text"], "tool:write_file tests/test_api.py")
        self.assertEqual(snapshot["progress_event_count"], 3)
        self.assertFalse(snapshot["terminal"])
        self.assertEqual(snapshot["title"], "implement-endpoint")

    def test_snapshot_completed_after_result(self) -> None:
        parent_id, child_id, events = self._events_for_child_lifecycle()
        snapshot = self.mod._task_snapshot_from_events(events)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None

        self.assertEqual(snapshot["task_id"], child_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["type"], TASK_RESULT)
        self.assertEqual(snapshot["parent_task_id"], parent_id)
        self.assertEqual(snapshot["latest_progress_text"], "tool:write_file tests/test_api.py")
        self.assertEqual(snapshot["progress_event_count"], 3)
        self.assertTrue(snapshot["terminal"])
        self.assertEqual(snapshot["text"], "Done: endpoint + tests")

    def test_empty_events_returns_none(self) -> None:
        self.assertIsNone(self.mod._task_snapshot_from_events([]))


class AgentStatusSummaryTests(unittest.TestCase):
    def test_offline(self) -> None:
        self.assertEqual(
            _agent_status_summary(label="@daniela", online=False, active_tasks=[]),
            "@daniela is offline",
        )

    def test_online_idle(self) -> None:
        self.assertEqual(
            _agent_status_summary(label="@daniela", online=True, active_tasks=[]),
            "@daniela is online and idle",
        )

    def test_online_active_with_progress(self) -> None:
        summary = _agent_status_summary(
            label="@daniela",
            online=True,
            active_tasks=[
                {
                    "status": "working",
                    "title": "implement-endpoint",
                    "latest_progress_text": "tool:write_file tests/test_api.py",
                    "parent_task_id": "task_parent_snap",
                }
            ],
        )
        self.assertEqual(
            summary,
            "@daniela is working on implement-endpoint: tool:write_file tests/test_api.py",
        )

    def test_online_active_without_progress_text(self) -> None:
        summary = _agent_status_summary(
            label="@sandra",
            online=True,
            active_tasks=[{"status": "working", "title": "api-fix"}],
        )
        self.assertEqual(summary, "@sandra is working on api-fix")


if __name__ == "__main__":
    unittest.main()
