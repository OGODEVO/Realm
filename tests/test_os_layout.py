"""Soft-clean layout hygiene checks (post OS layout reorg)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestOsLayout(unittest.TestCase):

    def test_drivers_mcp_present(self) -> None:
        for name in (
            "realm-mcp.py",
            "realm-agent-launcher.py",
            "realm-collaborator.py",
        ):
            path = ROOT / "drivers" / "mcp" / name
            self.assertTrue(path.is_file(), f"missing {path}")
            self.assertGreater(path.stat().st_size, 500, f"too small (stub?): {path}")

    def test_boot_shell_present(self) -> None:
        self.assertTrue((ROOT / "boot" / "docker-compose.yml").is_file())
        self.assertTrue((ROOT / "boot" / "network.sh").is_file())
        self.assertTrue((ROOT / "boot" / "realm.sh").is_file())

    def test_mesh_tools_stay_in_tools(self) -> None:
        for name in (
            "nba_tools.py",
            "search_tools.py",
            "nba_client.py",
            "odds_client.py",
            "team_lookup.py",
            "log_context.py",
        ):
            self.assertTrue((ROOT / "tools" / name).is_file(), f"mesh tool missing: {name}")

    def test_experiment_tools_in_distro(self) -> None:
        for name in (
            "mlb_finalize_player_layer.py",
            "mlb_live_update_layer.py",
            "mlb_sports_metric_layer.py",
            "olist_semantic_layer.py",
            "prepare_cuad_classification.py",
            "llm_ingest.py",
        ):
            self.assertTrue(
                (ROOT / "distro" / "tools" / name).is_file(),
                f"expected distro/tools/{name}",
            )
            self.assertFalse(
                (ROOT / "tools" / name).exists(),
                f"experiment tool still under tools/: {name}",
            )

    def test_experiments_artifacts_under_distro(self) -> None:
        if (ROOT / "experiments").exists():
            self.fail("root experiments/ still present after finish_moves")
        if (ROOT / "artifacts").exists():
            self.fail("root artifacts/ still present after finish_moves")
        # After moves they must exist under distro (if they existed originally)
        # Allow either present under distro or never existed
        self.assertTrue((ROOT / "distro").is_dir())

    def test_stackwise_status_quarantined(self) -> None:
        self.assertTrue((ROOT / "distro" / "STATUS.stackwise.md").is_file())
        root_status = ROOT / "STATUS.md"
        if root_status.is_file():
            text = root_status.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("stack-wise", text.lower())
            self.assertIn("distro/STATUS.stackwise.md", text)

    def test_distro_and_apps_readmes(self) -> None:
        self.assertTrue((ROOT / "distro" / "README.md").is_file())
        self.assertTrue((ROOT / "apps" / "README.md").is_file())
        self.assertTrue((ROOT / "docs").is_dir())


if __name__ == "__main__":
    unittest.main()
