# distro/ — non-kernel experiments

This tree is **not** the Realm kernel.

Realm OS (mental model):

| Layer | Where |
|-------|--------|
| Kernel / bus | `src/agentnet/` |
| Process table | `services/registry/` + Postgres |
| Drivers | `drivers/mcp/`, mesh-facing `tools/*` |
| Boot / init | `boot/` (compose, `network.sh`, `realm.sh`) |
| Userland apps | `apps/`, `services/`, gateways |
| **Distro / experiments** | **`distro/` (this directory)** |

## What lives here

- `experiments/` — training notebooks, CUAD baselines, throwaway scripts
- `artifacts/` — generated datasets, ontology dumps, LLM-ready profiles
- `tools/` — one-off domain scripts (MLB layers, Olist semantic layer, CUAD prep, `llm_ingest`) that are **not** imported by the agent mesh
- `STATUS.stackwise.md` — misfiled Stackwise tracker (not Realm)

## Do not treat as kernel

- Do not import `distro/*` from `src/agentnet`, registry, or mesh agents without an explicit product decision.
- Mesh agents still use `tools/nba_tools.py`, `tools/search_tools.py`, and their dependencies (`nba_client`, `odds_client`, `team_lookup`, `log_context`) under repo-root `tools/` — those stay put.

If something here graduates into product, move it to `drivers/`, `apps/`, or `src/` deliberately — do not grow the kernel by accident.
