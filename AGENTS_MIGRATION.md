# Agent Demos Migration

This repository is now network-focused.

## What moved

Agent demo/runtime code from `agents/` has been moved to:

- `realm-agents-playground` (separate repository)

## What stays here

- `src/agentnet` (SDK + node + CLI)
- `services/registry` (registry service)
- `docker` (NATS/Postgres/registry stack)
- operator and integration docs

## Compatibility window

Legacy agent launch entrypoints in this repo are now stubs:

- `start_mesh_trio.sh`
- `agents/agent_mesh_trio.py`

By default they print a "moved" message and exit.

Temporary override (for short migration window only):

```bash
ALLOW_LEGACY_AGENTS=1 ./start_mesh_trio.sh
ALLOW_LEGACY_AGENTS=1 python agents/agent_mesh_trio.py --config agents/config/mesh_agents.yaml
```

## Why

- Cleaner separation of concerns (network vs agent logic)
- Smaller operational surface for the network repo
- Easier versioned integration from external agent repos
