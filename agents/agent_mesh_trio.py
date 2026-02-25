from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mesh_config import load_mesh_config
from mesh_runtime import AgentRunner
from mesh_tools import ToolCatalog


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run 3-agent mesh skeleton with per-agent prompts and tools.")
    parser.add_argument(
        "--config",
        default="agents/config/mesh_agents.yaml",
        help="Path to YAML config file.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Optional agent key filter (repeatable). Example: --only agent_1 --only agent_2",
    )
    return parser


async def amain() -> int:
    args = _build_parser().parse_args()
    cfg = load_mesh_config(args.config)
    only = {item.strip() for item in args.only if item.strip()}

    tool_catalog = ToolCatalog()
    if tool_catalog.load_errors:
        print("tool module load warnings:")
        for err in tool_catalog.load_errors:
            print(f"- {err}")

    selected = [agent for agent in cfg.agents if not only or agent.key in only]
    if not selected:
        raise RuntimeError("No agents selected. Check --only values against config keys.")

    runners = [AgentRunner(profile=profile, defaults=cfg.defaults, tool_catalog=tool_catalog) for profile in selected]

    for runner in runners:
        await runner.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()

    for runner in reversed(runners):
        await runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
