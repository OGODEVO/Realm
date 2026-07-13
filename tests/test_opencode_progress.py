from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_opencode_agent():
    path = Path(__file__).resolve().parents[1] / "examples" / "opencode_realm_agent.py"
    spec = importlib.util.spec_from_file_location("opencode_realm_agent", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OpenCodeProgressMappingTests(unittest.TestCase):
    def test_part_progress_maps_tool_and_text(self) -> None:
        mod = _load_opencode_agent()
        tool = mod._part_progress_event(
            {
                "type": "tool",
                "name": "read_file",
                "status": "running",
                "input": {"path": "src/api.py"},
            }
        )
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool[0], "tool")
        self.assertIn("read_file", tool[1])

        text = mod._part_progress_event({"type": "text", "text": "Implementing endpoint"})
        self.assertEqual(text, ("text", "Implementing endpoint"))

        thinking = mod._part_progress_event({"type": "reasoning", "text": "hmm"})
        self.assertEqual(thinking, ("thinking", "hmm"))


if __name__ == "__main__":
    unittest.main()
