from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_registry_main():
    path = Path(__file__).resolve().parents[1] / "services" / "registry" / "main.py"
    spec = importlib.util.spec_from_file_location("realm_registry_main_role", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AgentRoleClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_registry_main()

    def test_mcp_harness_hidden_from_company(self) -> None:
        result = self.mod.classify_agent_role(
            capabilities=["mcp-bridge", "realm-tools"],
            metadata={"kind": "mcp-server"},
            username="medusa-bridge",
        )
        self.assertEqual(result["role"], "mcp-harness")
        self.assertFalse(result["company_visible"])

    def test_worker_visible(self) -> None:
        result = self.mod.classify_agent_role(
            capabilities=["llm", "coding-agent"],
            metadata={"kind": "opencode-llm-agent"},
            username="future-oasis-gpt55",
        )
        self.assertEqual(result["role"], "worker")
        self.assertTrue(result["company_visible"])

    def test_telegram_gateway(self) -> None:
        result = self.mod.classify_agent_role(
            capabilities=["human-gateway", "telegram"],
            metadata={},
            username="telegram-gateway",
        )
        self.assertEqual(result["role"], "human-gateway")
        self.assertTrue(result["company_visible"])

    def test_coding_agent_capability_is_worker(self) -> None:
        result = self.mod.classify_agent_role(
            capabilities=["coding-agent"],
            metadata={"kind": "coding-agent"},
            username="coder",
        )
        self.assertEqual(result["role"], "worker")
        self.assertTrue(result["company_visible"])

    def test_other_defaults_visible(self) -> None:
        result = self.mod.classify_agent_role(
            capabilities=["notes"],
            metadata={"kind": "sensor"},
            username="sensor-1",
        )
        self.assertEqual(result["role"], "other")
        self.assertTrue(result["company_visible"])

    def test_dedupe_enriches_role_and_session_count(self) -> None:
        AgentInfo = self.mod.AgentInfo
        a1 = AgentInfo(
            agent_id="s1",
            name="Bridge",
            account_id="acct_bridge",
            username="medusa-bridge",
            session_tag="sess-a",
            capabilities=["mcp-bridge", "realm-tools"],
            metadata={"kind": "mcp-server"},
            last_seen="2026-01-01T00:00:01Z",
        )
        a2 = AgentInfo(
            agent_id="s2",
            name="Bridge",
            account_id="acct_bridge",
            username="medusa-bridge",
            session_tag="sess-b",
            capabilities=["mcp-bridge", "realm-tools"],
            metadata={"kind": "mcp-server"},
            last_seen="2026-01-01T00:00:02Z",
        )
        rows = self.mod._dedupe_online_agents([a1, a2])
        self.assertEqual(len(rows), 1)
        meta = rows[0].metadata
        self.assertEqual(meta["session_count"], 2)
        self.assertEqual(meta["role"], "mcp-harness")
        self.assertFalse(meta["company_visible"])


if __name__ == "__main__":
    unittest.main()
