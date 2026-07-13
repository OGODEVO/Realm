#!/usr/bin/env python3
"""Offline demo of the Sandra → Daniela job chain payloads.

Shows the standard parent_task_id + progress protocol without requiring NATS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentnet.task_protocol import (  # noqa: E402
    build_task_assign,
    build_task_progress,
    build_task_result,
    new_task_id,
    parent_task_id_from_payload,
)


def main() -> int:
    sandra_task = new_task_id("sandra")
    daniela_task = new_task_id("daniela")

    boss_to_sandra = build_task_assign(
        task_id=sandra_task,
        text="Ship the API fix this week",
        coordinator="boss",
        title="api-fix",
    )
    sandra_to_daniela = build_task_assign(
        task_id=daniela_task,
        text="Implement the endpoint and tests",
        coordinator="sandra",
        title="implement-endpoint",
        parent_task_id=sandra_task,
    )
    progress = build_task_progress(
        task_id=daniela_task,
        text="Writing handler and tests",
        percent=40,
        phase="coding",
    )
    done = build_task_result(task_id=daniela_task, text="Endpoint + tests ready", status="completed")

    print("=== Boss → Sandra ===")
    print(json.dumps(boss_to_sandra, indent=2))
    print("\n=== Sandra → Daniela (child) ===")
    print(json.dumps(sandra_to_daniela, indent=2))
    print(f"\nparent_task_id_from_payload => {parent_task_id_from_payload(sandra_to_daniela)!r}")
    print("\n=== Daniela progress ===")
    print(json.dumps(progress, indent=2))
    print("\n=== Daniela done ===")
    print(json.dumps(done, indent=2))
    print(
        "\nLive network: ./network.sh status @daniela  |  "
        "list children: agentnet tasks --parent-task-id",
        sandra_task,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
