# agent.md — Context for the next agent working on this repo

**Audience:** coding agents / humans continuing development on **Realm (AgentNet)**.  
**Not** the mesh operating guide for workers on the network (that is [AGENTS.md](AGENTS.md)).

Load this first when you open the repo. Then skim the “Do not break” and “Current branch work” sections before editing.

---

## What this project is

**Realm / AgentNet** = multi-agent **company bus** over NATS:

- Durable identities (`@username` / `account_id`)
- Messaging, threads, receipts
- **Jobs:** `task.assign` → `task.progress` → `task.result|blocked|failed`
- Registry + Postgres as shared truth for presence + task snapshots
- MCP bridges so CLI agents (Codex / Grok / OpenCode) can coordinate

**Package:** `agentnet-realm` **0.1.1** (`pyproject.toml`)  
**Hub (this machine):** NATS `127.0.0.1:4222` + Tailscale `100.84.141.84:4222`  
**Remote clients:** same auth, URL `nats://agentnet_secret_token@100.84.141.84:4222`

Product vision (owner): permanent specialist agents, horizontal/vertical delegation, live “what is X doing?”, forward-compatible brains (OpenCode / `codex exec` / `grok` headless) behind one job contract.

---

## Doc map (do not confuse)

| File | For whom | Purpose |
|------|----------|---------|
| **This file (`agent.md`)** | **Next repo agent** | Load context, state, where to edit |
| [AGENTS.md](AGENTS.md) | Agents **on the mesh** | How to use the network (jobs ≠ chat) |
| [skills.md](skills.md) | Mesh + coordinators | Skills/capabilities map |
| [ORCHESTRATION.md](ORCHESTRATION.md) | Mesh patterns | parent_task_id, pipeline, parallel |
| [CHANGELOG.md](CHANGELOG.md) | Humans | 0.1.x notes |
| [NETWORK_CLI_GUIDE.md](NETWORK_CLI_GUIDE.md) | Ops | CLI deep dive |
| [README.md](README.md) | Everyone | Quickstart + 0.1 header |

---

## Repo layout (high signal)

```text
src/agentnet/           # SDK, node, registry client, task_protocol, CLI
services/registry/      # Registry service (presence, threads, task snapshots) — Docker
services/agent-template/# Durable worker homes: start-opencode-agent.sh, start-cli-agent.sh
mcp-server/             # realm-mcp.py, realm-agent-launcher.py, realm-collaborator.py
examples/
  opencode_realm_agent.py   # OpenCode-backed worker
  cli_realm_agent.py        # Codex / Grok headless worker
docker/docker-compose.yml   # nats + postgres + registry
network.sh                  # thin CLI wrapper (list, status, tasks, …)
tests/                      # unittest (run with PYTHONPATH=src)
scripts/smoke_task_loop.py  # offline assign→progress→result smoke
```

---

## Architecture (mental model)

```text
Coordinator (Codex/Grok/OC + realm MCP)
        │  delegate_task / agent_status
        ▼
     NATS hub  ←── workers register (hello + account inbox)
        │
   registry service ── Postgres (sessions, messages, task events derived)
        │
 Workers: OpenCode wrapper | cli_realm_agent (codex exec | grok)
```

**Job contract every worker must honor:**

1. Register `@username` + capabilities  
2. Receive `task.assign`  
3. Emit `task.progress`  
4. One terminal: result / blocked / failed  
5. Optional short chat / STATE  

Implementation surface:

- Protocol helpers: `src/agentnet/task_protocol.py`
- SDK: `src/agentnet/sdk.py` (`delegate_task`, `report_progress`, `agent_status`, `list_tasks`)
- Registry client: `src/agentnet/registry.py` (`get_agent_status`, list filters)
- Registry server: `services/registry/main.py` (snapshots, online **dedupe**, **role** classification)
- MCP: `mcp-server/realm-mcp.py`

---

## Current branch / work status

**Branch:** `codex/realm-ack-timeout-recovery` (plus large uncommitted 0.1 orchestration work)

**Tests (as of last handoff):**  
`PYTHONPATH=src python3 -m unittest discover -s tests -q` → **43 OK**

### Shipped in this working tree (0.1 focus) — treat as intentional

| Area | What |
|------|------|
| Tasks | `parent_task_id`, progress fields on snapshots, `report_progress` |
| Visibility | `agent_status`, CLI `agent-status`, `./network.sh status` |
| Presence | Online **dedupe** by account; `session_count`; `role` + `company_visible` |
| MCP | `agent_status`, `report_progress`, `parent_task_id` on delegate; ACK not required for assign |
| Workers | OpenCode progress → real `task.progress`; long work timeout; **cli** Codex/Grok agent |
| Docs | AGENTS.md, ORCHESTRATION.md, CHANGELOG, skills.md, README 0.1 header |
| Smoke | `scripts/smoke_task_loop.py` |

### Hub ops already done on this machine

- Registry Docker image rebuilt with dedupe + roles (needs rebuild again if you change `services/registry/main.py`)
- NATS/Postgres typically left running

### Not done / 0.2 candidates (do not claim fixed)

- Exclusive **job lease** (multi-session same identity can still race)
- Auto-hide mcp-harness from default list (labeled only today)
- Pretty stand-up UI board
- Full OpenCode path inside `cli_realm_agent.py` (intentionally points at `opencode_realm_agent.py`)
- Commit/push of this whole 0.1 set (many files still modified/untracked)

---

## Workspace hygiene (launcher)

**Cleaned (this hub):** stale `realm-worker-a`…`e` under  
`Documents/Realm/.realm/agent-launcher/agents/` removed (PIDs were dead; all pointed at the same repo).

| Pattern | Clean? |
|---------|--------|
| One agent writes in one workspace | **Yes** |
| Many agents **read/review** one workspace | **OK** if they do not edit the same files |
| Many agents **write** the same workspace | **No** — race on files/git; old a–e setup was this |
| Parallel writers | Separate **git worktrees** (or clones), one per agent |

Launcher still *allows* the same `workspace=` for multiple agents; that is a footgun, not a feature. Prefer:

```text
@coder    workspace=/path/to/repo              # or worktree for feature A
@reviewer workspace=/path/to/repo              # read-focused; avoid concurrent edits
@coder-b  workspace=/path/to/repo-worktrees/b  # parallel feature B
```

Agent **home** (under `agent-launcher/agents/<id>/`) is always private; only **workspace** is the shared/project tree.

---

## Do not break

1. **Account inbox routing** — messaging is account-based; usernames resolve to accounts.  
2. **Task event derivation** — tasks are reconstructed from message payloads in Postgres (`task.*` types), not a separate heavy task table only. Snapshot helpers live in registry `main.py`.  
3. **Chat vs jobs** — do not “fix” timeouts by making `ask_text` the job system again.  
4. **Worker progress must be `task.progress`** — not custom chat JSON only (that was the silent-progress bug).  
5. **MCP bridges ≠ permanent specialists** — avoid making every worker spawn nested Realm MCP (`--pure` OpenCode rule for task workers).  
6. **Remote hub URL** — remotes use Tailscale NATS; local default `localhost:4222`.

---

## How to verify after changes

```bash
cd /Users/a.developer/Documents/Realm
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/smoke_task_loop.py

# live hub (if docker up)
./network.sh list
./network.sh status @future-oasis-gpt55
./network.sh metrics

# if you changed services/registry/main.py
docker compose -f docker/docker-compose.yml up -d --build registry
# wait for agents to re-hello, then list again
```

CLI workers:

```bash
# Codex / Grok — see services/agent-template/README.md + env.cli.example
REALM_AGENT_HOME=~/.local/share/codex-worker services/agent-template/start-cli-agent.sh
```

Headless brains on this machine:

- `codex` → **`codex exec …`**
- `grok` → **`grok -p …`** / agent headless flags  
- OpenCode → existing serve + wrapper

---

## Where to edit for common goals

| Goal | Touch |
|------|--------|
| Job payload / parent / progress shape | `src/agentnet/task_protocol.py` + registry snapshot + tests |
| Coordinator SDK API | `src/agentnet/sdk.py` |
| Status / list_tasks client | `src/agentnet/registry.py` |
| Online roster / roles / GC | `services/registry/main.py` → rebuild Docker |
| MCP tools | `mcp-server/realm-mcp.py` |
| OpenCode worker | `examples/opencode_realm_agent.py` |
| Codex/Grok worker | `examples/cli_realm_agent.py` |
| Human CLI | `src/agentnet/__main__.py`, `network.sh` |
| Mesh law for agents using the network | `AGENTS.md` (not this file) |

---

## Suggested next work (if continuing product)

1. **Job lease / claim** so multi-session identities don’t double-run a task  
2. Default `list_online` filter or MCP helper: company workers only  
3. Commit coherent 0.1.1 stack (exclude unrelated `artifacts/`, mlb/olist experiment tools unless asked)  
4. Live smoke: `delegate_task` → `@codex-worker` → `task_status` with progress  
5. Optional: launcher support for `REALM_RUNTIME=codex|grok` not only OpenCode  

---

## Env / secrets note

- Default NATS token in compose/docs: `agentnet_secret_token` (local/dev style)  
- Agent homes: `~/.local/share/<agent-id>/.env` (identity, ports, models)  
- Do not commit real tokens from personal `.env` files  

---

## One-line handoff

**Realm 0.1 is a working multi-agent job bus (delegate → progress → result + agent_status + multi-runtime workers); this tree has that mostly implemented and tested (43 tests) but not fully committed—extend carefully, preserve the job contract, rebuild registry Docker when changing presence/task indexing.**
