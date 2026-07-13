from __future__ import annotations

import json
import unittest

from agentnet.task_protocol import (
    TASK_ASSIGN,
    TASK_FAILED,
    TASK_PROGRESS,
    TASK_RESULT,
    build_task_assign,
    build_task_progress,
    build_task_result,
    decode_task_payload,
    is_terminal_task_payload,
    parent_task_id_from_payload,
    task_id_from_payload,
    task_type,
)
from agentnet.registry import _agent_status_summary


class TaskProtocolTests(unittest.TestCase):
    def test_builds_assign_payload_with_stable_type_and_task_id(self) -> None:
        payload = build_task_assign(
            task_id="task_123",
            text="Fix the backend",
            coordinator="medusa-bridge",
            title="backend fix",
        )

        self.assertEqual(payload["type"], TASK_ASSIGN)
        self.assertEqual(task_id_from_payload(payload), "task_123")
        self.assertEqual(task_type(payload), TASK_ASSIGN)
        self.assertIs(decode_task_payload(payload), payload)

    def test_parent_task_id_on_child_assign(self) -> None:
        payload = build_task_assign(
            task_id="task_child",
            text="Implement endpoint",
            coordinator="sandra",
            title="implement",
            parent_task_id="task_parent",
        )
        self.assertEqual(payload["parent_task_id"], "task_parent")
        self.assertEqual(parent_task_id_from_payload(payload), "task_parent")
        self.assertEqual(parent_task_id_from_payload({"type": TASK_ASSIGN, "task_id": "x"}), "")

    def test_progress_includes_percent_and_phase(self) -> None:
        payload = build_task_progress(
            task_id="task_123",
            text="coding",
            percent=40,
            phase="implement",
        )
        self.assertEqual(payload["type"], TASK_PROGRESS)
        self.assertEqual(payload["metadata"]["percent"], 40.0)
        self.assertEqual(payload["metadata"]["phase"], "implement")

    def test_terminal_result_types_are_explicit(self) -> None:
        completed = build_task_result(task_id="task_123", text="Done")
        failed = build_task_result(task_id="task_123", text="Nope", status="failed")

        self.assertEqual(completed["type"], TASK_RESULT)
        self.assertEqual(failed["type"], TASK_FAILED)
        self.assertTrue(is_terminal_task_payload(completed))
        self.assertTrue(is_terminal_task_payload(failed))

    def test_decodes_task_payload_from_json_string(self) -> None:
        payload = json.dumps({"type": TASK_RESULT, "task_id": "task_123", "text": "Done"})

        self.assertEqual(task_type(payload), TASK_RESULT)
        self.assertEqual(task_id_from_payload(payload), "task_123")

    def test_registry_snapshot_includes_progress_history(self) -> None:
        """Import registry helpers offline and require progress_history on snapshot."""
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "services" / "registry" / "main.py"
        spec = importlib.util.spec_from_file_location("realm_registry_main_hist", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        rows = []
        for i, payload in enumerate(
            [
                build_task_assign(task_id="task_h", text="do it", coordinator="boss"),
                build_task_progress(task_id="task_h", text="ack", phase="ack"),
                build_task_progress(task_id="task_h", text="tool:x", phase="tool"),
                build_task_result(task_id="task_h", text="done"),
            ]
        ):
            rows.append(
                {
                    "payload": payload,
                    "message_id": f"m{i}",
                    "sent_at": f"2026-07-12T10:0{i}:00Z",
                    "received_at": f"2026-07-12T10:0{i}:00Z",
                    "from_account_id": "a",
                    "to_account_id": "b",
                    "thread_id": "t",
                    "parent_message_id": None,
                    "to_agent": None,
                }
            )
        events = [mod._task_event_from_message_row(r) for r in rows]
        snap = mod._task_snapshot_from_events(events)
        assert snap is not None
        self.assertEqual(snap["progress_event_count"], 2)
        self.assertEqual(len(snap["progress_history"]), 2)
        self.assertEqual(snap["progress_history"][0]["phase"], "ack")
        self.assertEqual(snap["progress_history"][1]["phase"], "tool")
        self.assertEqual(len(snap["event_history"]), 4)
        self.assertTrue(snap["terminal"])

    def test_agent_status_summary_lines(self) -> None:
        self.assertEqual(_agent_status_summary(label="@d", online=False, active_tasks=[]), "@d is offline")
        self.assertEqual(_agent_status_summary(label="@d", online=True, active_tasks=[]), "@d is online and idle")
        summary = _agent_status_summary(
            label="@daniela",
            online=True,
            active_tasks=[
                {
                    "status": "working",
                    "title": "implement-endpoint",
                    "latest_progress_text": "Writing tests",
                }
            ],
        )
        self.assertIn("Writing tests", summary)
        self.assertIn("@daniela", summary)


class TaskSnapshotTests(unittest.TestCase):
    def test_snapshot_parent_and_progress_fields(self) -> None:
        # Import registry pure helpers without requiring live services.
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "services" / "registry" / "main.py"
        spec = importlib.util.spec_from_file_location("realm_registry_main", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        # Minimal stubs so import side effects for optional deps do not matter.
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"registry main import failed: {exc}")

        events = [
            {
                "task_id": "task_child",
                "type": TASK_ASSIGN,
                "status": "assigned",
                "title": "implement",
                "text": "do the work",
                "thread_id": "thread_1",
                "message_id": "m1",
                "to_account_id": "acct_daniela",
                "from_account_id": "acct_sandra",
                "sent_at": "2026-07-09T10:00:00Z",
                "payload": {
                    "type": TASK_ASSIGN,
                    "task_id": "task_child",
                    "parent_task_id": "task_parent",
                    "coordinator": "sandra",
                    "text": "do the work",
                    "title": "implement",
                },
            },
            {
                "task_id": "task_child",
                "type": TASK_PROGRESS,
                "status": "working",
                "text": "halfway there",
                "thread_id": "thread_1",
                "message_id": "m2",
                "to_account_id": "acct_sandra",
                "from_account_id": "acct_daniela",
                "sent_at": "2026-07-09T10:05:00Z",
                "payload": {
                    "type": TASK_PROGRESS,
                    "task_id": "task_child",
                    "text": "halfway there",
                },
            },
        ]
        snapshot = mod._task_snapshot_from_events(events)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["parent_task_id"], "task_parent")
        self.assertEqual(snapshot["coordinator"], "sandra")
        self.assertEqual(snapshot["latest_progress_text"], "halfway there")
        self.assertEqual(snapshot["progress_event_count"], 1)
        self.assertFalse(snapshot["terminal"])


if __name__ == "__main__":
    unittest.main()
