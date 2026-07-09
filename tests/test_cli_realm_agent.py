from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "examples" / "cli_realm_agent.py"
SPEC = importlib.util.spec_from_file_location("cli_realm_agent", MODULE_PATH)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class ParseRuntimeTests(unittest.TestCase):
    def test_defaults_and_aliases(self) -> None:
        self.assertEqual(agent.parse_runtime(None), "codex")
        self.assertEqual(agent.parse_runtime("  Grok "), "grok")
        self.assertEqual(agent.parse_runtime("opencode"), "opencode")

    def test_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            agent.parse_runtime("claude")


class BuildCmdTests(unittest.TestCase):
    def test_codex_cmd_default_sandbox(self) -> None:
        cmd = agent.build_codex_cmd(
            "do the work",
            bin_path="/opt/homebrew/bin/codex",
            workdir="/tmp/ws",
            model="o3",
            sandbox="workspace-write",
            full_auto=False,
            json_events=False,
            skip_git_check=True,
        )
        self.assertEqual(cmd[0], "/opt/homebrew/bin/codex")
        self.assertEqual(cmd[1], "exec")
        self.assertIn("-s", cmd)
        self.assertIn("workspace-write", cmd)
        self.assertIn("-C", cmd)
        self.assertIn("/tmp/ws", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("o3", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(cmd[-1], "do the work")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_codex_full_auto_skips_sandbox_flag(self) -> None:
        cmd = agent.build_codex_cmd(
            "go",
            full_auto=True,
            sandbox="workspace-write",
            skip_git_check=False,
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("-s", cmd)

    def test_grok_agent_mode_positional_prompt(self) -> None:
        cmd = agent.build_grok_cmd(
            "fix it",
            bin_path="grok",
            workdir="/tmp/ws",
            model="grok-4",
            always_approve=True,
            output_format="plain",
            mode="agent",
        )
        self.assertEqual(cmd[0], "grok")
        self.assertIn("--always-approve", cmd)
        self.assertIn("--cwd", cmd)
        self.assertIn("/tmp/ws", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("grok-4", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("plain", cmd)
        self.assertEqual(cmd[-1], "fix it")
        self.assertNotIn("-p", cmd)

    def test_grok_single_mode_uses_dash_p(self) -> None:
        cmd = agent.build_grok_cmd(
            "one shot",
            always_approve=False,
            mode="single",
            output_format="json",
        )
        self.assertNotIn("--always-approve", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("one shot", cmd)

    def test_build_brain_cmd_routes_and_blocks_opencode(self) -> None:
        codex = agent.build_brain_cmd("codex", "hi")
        self.assertEqual(codex[1], "exec")
        with self.assertRaises(RuntimeError) as ctx:
            agent.build_brain_cmd("opencode", "hi")
        self.assertIn("opencode_realm_agent.py", str(ctx.exception))


class PromptAndMarkersTests(unittest.TestCase):
    def test_task_prompt_includes_contract_and_request(self) -> None:
        prompt = agent.build_task_prompt(
            "Ship the feature",
            system_prompt="You are a worker.",
            execution_contract="Finish with markers.",
            task_id="task_1",
            task_title="Ship",
            parent_task_id="task_0",
            thread_id="thread_x",
            metadata={"k": "v"},
        )
        self.assertIn("You are a worker.", prompt)
        self.assertIn("Finish with markers.", prompt)
        self.assertIn("task_id: task_1", prompt)
        self.assertIn("parent_task_id: task_0", prompt)
        self.assertIn("Ship the feature", prompt)

    def test_markers(self) -> None:
        self.assertIsNone(agent.task_answer_status("still going"))
        self.assertEqual(
            agent.task_answer_status("done [REALM_TASK_COMPLETE]"), "complete"
        )
        self.assertEqual(
            agent.task_answer_status("need input [REALM_TASK_BLOCKED]"), "blocked"
        )
        self.assertEqual(
            agent.clean_task_answer("done\n[REALM_TASK_COMPLETE]"), "done"
        )

    def test_classify_progress_line(self) -> None:
        phase, text = agent.classify_progress_line('{"type":"tool","name":"read_file"}')
        self.assertEqual(phase, "tool")
        self.assertIn("read_file", text)
        phase2, text2 = agent.classify_progress_line("plain status line")
        self.assertEqual(phase2, "text")
        self.assertIn("plain", text2)

    def test_env_bool(self) -> None:
        import os

        os.environ.pop("CLI_AGENT_TEST_BOOL", None)
        self.assertTrue(agent.env_bool("CLI_AGENT_TEST_BOOL", default=True))
        os.environ["CLI_AGENT_TEST_BOOL"] = "yes"
        self.assertTrue(agent.env_bool("CLI_AGENT_TEST_BOOL", default=False))
        os.environ["CLI_AGENT_TEST_BOOL"] = "0"
        self.assertFalse(agent.env_bool("CLI_AGENT_TEST_BOOL", default=True))
        os.environ.pop("CLI_AGENT_TEST_BOOL", None)


if __name__ == "__main__":
    unittest.main()
