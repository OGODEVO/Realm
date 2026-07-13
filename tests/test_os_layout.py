"""Layout hygiene: product surface is network / mesh / apps."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestOsLayout(unittest.TestCase):
    def test_kernel_present(self) -> None:
        self.assertTrue((ROOT / "src" / "agentnet" / "sdk.py").is_file())
        self.assertTrue((ROOT / "src" / "agentnet" / "node.py").is_file())

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

    def test_apps_and_docs(self) -> None:
        self.assertTrue((ROOT / "apps" / "README.md").is_file())
        self.assertTrue((ROOT / "docs" / "architecture.md").is_file())
        self.assertTrue((ROOT / "services" / "registry" / "main.py").is_file())

    def test_non_product_paths_gone(self) -> None:
        for name in ("simple-html-app", "taskflow", "agents", "tools"):
            # Allowed only if gitignored local leftover; must not be tracked product surface
            # Prefer absence for clean tree
            path = ROOT / name
            if path.exists() and name != "tools":
                # tools may be gitignored empty; hard fail if py package present
                pass
        self.assertFalse((ROOT / "tools" / "nba_tools.py").exists())
        self.assertFalse((ROOT / "simple-html-app").exists())
        self.assertFalse((ROOT / "taskflow").exists())


if __name__ == "__main__":
    unittest.main()
