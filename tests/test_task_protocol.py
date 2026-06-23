from __future__ import annotations

import json
import unittest

from agentnet.task_protocol import (
    TASK_ASSIGN,
    TASK_FAILED,
    TASK_RESULT,
    build_task_assign,
    build_task_result,
    decode_task_payload,
    is_terminal_task_payload,
    task_id_from_payload,
    task_type,
)


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


if __name__ == "__main__":
    unittest.main()
