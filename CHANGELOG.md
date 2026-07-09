# Changelog

All notable changes to Realm (AgentNet) are documented here.

## [0.1.0] — 2026-07-09

First public 0.1 release focused on multi-agent task orchestration: permanent agents, jobs over chat, and operator-visible progress.

### Added

- **`parent_task_id`** — vertical re-delegation chains (coordinator → mid-tier → worker) on `delegate_task` / task events
- **`agent_status`** — presence + active/recent tasks + latest progress summary for an agent (`./network.sh status @user`)
- **`task.progress` visibility** — registry and MCP expose latest progress text on `task_status` / `list_tasks` / `agent_status`
- **`report_progress` MCP tool** — workers publish structured progress while a job is open
- **Online dedupe** — one logical row per identity when listing online agents (session-aware, not spam per connection)
- **[AGENTS.md](AGENTS.md)** — required operating guide for every agent on the mesh
- **[ORCHESTRATION.md](ORCHESTRATION.md)** — Sandra/Daniela-style job patterns and the stable loop

### Fixed

- **Worker progress** — progress events attach correctly to the active `task_id` and surface through registry lookup
- **Ack defaults** — task result delivery no longer requires a coordinator-side ack when the registry already has the terminal result (OpenCode worker defaults)

### Package

- PyPI / install name: `agentnet-realm` **0.1.0** (subsequent polish: **0.1.1**)

## [0.1.1] — 2026-07-09

### Added

- **Roster roles** — `metadata.role` + `company_visible` on online list (`worker`, `human-gateway`, `orchestrator`, `mcp-harness`)
- **Offline smoke** — `scripts/smoke_task_loop.py` and orchestration flow tests for assign → progress → result

### Docs

- README 0.1 top section (agent company network, stable loop, `network.sh` commands)
- MCP tool list aligned with `realm-mcp` (`agent_status`, `report_progress`, `parent_task_id`)
- `agent.md` clarified as fleet notes; operators use **AGENTS.md**
