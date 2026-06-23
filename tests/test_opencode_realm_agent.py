from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


MODULE_PATH = Path(__file__).parents[1] / "examples" / "opencode_realm_agent.py"
SPEC = importlib.util.spec_from_file_location("opencode_realm_agent", MODULE_PATH)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class ExtractCurrentTurnTextTests(unittest.TestCase):
    def test_status_comes_only_from_explicit_marker(self) -> None:
        self.assertIsNone(agent.task_answer_status("Let me check the tools."))
        self.assertEqual(
            agent.task_answer_status("Finished. [REALM_TASK_COMPLETE]"),
            "complete",
        )
        self.assertEqual(
            agent.task_answer_status("Which account? [REALM_TASK_BLOCKED]"),
            "blocked",
        )

    def test_control_marker_is_removed_from_user_answer(self) -> None:
        self.assertEqual(
            agent.clean_task_answer("Finished.\n[REALM_TASK_COMPLETE]"),
            "Finished.",
        )

    def test_ignores_assistant_text_from_earlier_turns(self) -> None:
        data = {
            "messages": [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "old"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "stale"}]},
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "new"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "fresh"}]},
            ]
        }

        self.assertEqual(agent.extract_current_turn_text(data), "fresh")

    def test_combines_text_across_tool_steps(self) -> None:
        data = {
            "messages": [
                {"info": {"role": "user"}, "parts": []},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Checking."}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "tool"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Finished."}]},
            ]
        }

        self.assertEqual(
            agent.extract_current_turn_text(data), "Checking.\n\nFinished."
        )


class AskOpenCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_status_continues_until_agent_declares_complete(self) -> None:
        run = AsyncMock(
            side_effect=[
                "Let me check the available notification tools.",
                "I checked them. Email and webhook notifications are available. "
                "[REALM_TASK_COMPLETE]",
            ]
        )

        with patch.object(agent, "ask_opencode_resilient", run):
            answer = await agent.ask_opencode_until_complete(
                "Find the notification tools",
                realm_thread_id="thread_same",
                session_map={"thread_same": "ses_live"},
            )

        self.assertEqual(
            answer,
            "I checked them. Email and webhook notifications are available.",
        )
        self.assertEqual(run.await_count, 2)
        self.assertIn("task is still active", run.await_args_list[1].args[0])

    async def test_turn_ceiling_is_configurable_safety_boundary(self) -> None:
        run = AsyncMock(side_effect=["still working", "still working"])

        with (
            patch.object(agent, "ask_opencode_resilient", run),
            patch.object(agent, "MAX_AGENT_TURNS", 2),
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeded 2 turns"):
                await agent.ask_opencode_until_complete(
                    "Do the work",
                    realm_thread_id="thread_same",
                    session_map={"thread_same": "ses_live"},
                )

        self.assertEqual(run.await_count, 2)

    async def test_corrupt_existing_session_is_replaced_on_same_thread(self) -> None:
        sessions = {"thread_same": "ses_broken"}
        run = AsyncMock(
            side_effect=[
                RuntimeError(
                    "OpenCode completed without returning a final text response"
                ),
                "recovered answer",
            ]
        )

        with (
            patch.object(agent, "ask_opencode", run),
            patch.object(agent, "save_session_map") as save,
        ):
            answer = await agent.ask_opencode_resilient(
                "hello",
                realm_thread_id="thread_same",
                session_map=sessions,
            )

        self.assertEqual(answer, "recovered answer")
        self.assertNotIn("thread_same", sessions)
        self.assertEqual(run.await_count, 2)
        save.assert_called_once_with(sessions)

    async def test_timeout_does_not_replay_existing_session(self) -> None:
        sessions = {"thread_same": "ses_slow"}
        run = AsyncMock(side_effect=RuntimeError("OpenCode timed out after 60 seconds"))

        with patch.object(agent, "ask_opencode", run):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await agent.ask_opencode_resilient(
                    "hello",
                    realm_thread_id="thread_same",
                    session_map=sessions,
                )

        self.assertEqual(run.await_count, 1)
        self.assertEqual(sessions["thread_same"], "ses_slow")

    async def test_cancelled_export_reaps_child_process(self) -> None:
        async def block_forever() -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        proc = Mock()
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=block_forever)
        proc.wait = AsyncMock(return_value=0)

        task = asyncio.create_task(
            agent.communicate_with_timeout(proc, timeout=60)
        )
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        proc.terminate.assert_called_once_with()
        proc.wait.assert_awaited_once_with()

    async def test_keeps_single_first_output_event(self) -> None:
        event = {
            "sessionID": "ses_test",
            "type": "text",
            "part": {"text": "single-line answer"},
        }
        proc = AsyncMock()
        proc.stdout.readline.return_value = (json.dumps(event) + "\n").encode()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0

        with patch.object(
            agent.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        ):
            answer = await agent.ask_opencode(
                "hello", realm_thread_id=None, session_map={}
            )

        self.assertEqual(answer, "single-line answer")


if __name__ == "__main__":
    unittest.main()
