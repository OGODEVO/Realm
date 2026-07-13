"""Unit tests for gateway helpers (no NATS, no uvicorn)."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _load_main():
    path = ROOT / "apps" / "gateway" / "main.py"
    spec = importlib.util.spec_from_file_location("realm_gateway_main", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # fastapi may be missing in minimal env — skip if so
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(f"missing dependency: {exc}") from exc
    return mod


class GatewayUnitTests(unittest.TestCase):
    def test_demo_routes_cover_five_endpoints(self) -> None:
        mod = _load_main()
        self.assertEqual(
            set(mod.DEMO_ROUTES),
            {
                "orders",
                "refunds",
                "inventory_low",
                "support_tickets",
                "shipping_delay",
            },
        )
        for target in mod.DEMO_ROUTES.values():
            self.assertTrue(target.startswith("@"))

    def test_api_keys_from_env(self) -> None:
        mod = _load_main()
        old = os.environ.get("GATEWAY_API_KEYS")
        try:
            os.environ["GATEWAY_API_KEYS"] = "alpha, beta , "
            self.assertEqual(mod._api_keys(), {"alpha", "beta"})
        finally:
            if old is None:
                os.environ.pop("GATEWAY_API_KEYS", None)
            else:
                os.environ["GATEWAY_API_KEYS"] = old


if __name__ == "__main__":
    unittest.main()
