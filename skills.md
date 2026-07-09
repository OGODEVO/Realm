# skills.md — Agent-first skills & capabilities map

> Skills are what an **agent can do or call**.  
> Capabilities are how you **advertise** that on the mesh.  
> Think agent-first: “What can I offer? What can I ask of others?”

Required network law: [AGENTS.md](AGENTS.md) · Repo handoff: [agent.md](agent.md)

---

## 0. Point skills at real CLI configs (this machine)

Realm network tools are **MCP servers**. Each CLI loads them from its own config.  
**Workers** use headless binaries + Realm wrapper env — not these coordinator configs.

| CLI | Config file | Skills / plugins dir | Binary |
|-----|-------------|----------------------|--------|
| **OpenCode** | `~/.config/opencode/opencode.json` | `~/.config/opencode/skills/` | `opencode` |
| **Codex** | `~/.codex/config.toml` | (project `AGENTS.md`; plugins under Codex home) | `codex` → headless **`codex exec`** |
| **Grok** | `~/.grok/config.toml` | `~/.grok/skills/` (+ `~/.grok/bundled/`) | `grok` → headless **`grok -p`** / agent |

Repo copies of Realm MCP scripts (what configs should invoke):

| MCP server | Script |
|------------|--------|
| `realm` | `/Users/a.developer/Documents/Realm/mcp-server/realm-mcp.py` |
| `realm-agent-launcher` | `.../mcp-server/realm-agent-launcher.py` |
| `realm-collaborator` | `.../mcp-server/realm-collaborator.py` |

Shared env almost always:

```bash
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222
# remote machines:
# REALM_NATS_URL=nats://agentnet_secret_token@100.84.141.84:4222
```

### Model defaults (this machine)

| CLI | Where model is set | Your target | How |
|-----|--------------------|-------------|-----|
| **Codex** | `~/.codex/config.toml` → top-level `model = "..."` | **`gpt-5.6-terra`** | Edit that line (or one-shot: `codex exec -m gpt-5.6-terra "…"`) |
| **Grok** | `~/.grok/config.toml` → `[models] default = "..."` | **`grok-4.5`** | Edit that (or one-shot: `grok -m grok-4.5 -p "…"`) |
| **OpenCode** | per-agent `opencode.json` / server model flags | whatever OC is configured for | Worker env `OPENCODE_MODEL=…` |

**Realm workers** (headless) do not always inherit CLI TUI defaults unless you set:

```bash
# ~/.local/share/<worker>/.env
CODEX_MODEL=gpt-5.6-terra    # → codex exec -m …
GROK_MODEL=grok-4.5          # → grok -m …
```

(`examples/cli_realm_agent.py` passes `-m` when those env vars are set.)

Reasoning effort (Codex only): `model_reasoning_effort = "high"` in `~/.codex/config.toml`.

### OpenCode — `~/.config/opencode/opencode.json`

MCP block keys already used on this hub:

| Key in `mcp` | Points at |
|--------------|-----------|
| `realm` | `mcp-server/realm-mcp.py` + `REALM_NATS_URL` |
| `realm-agent-launcher` | launcher script + `REALM_REPO`, `REALM_AGENT_LAUNCHER_HOME` |
| `realm-collaborator` | collaborator script + blob dir |

Also: `~/.config/opencode/skills/` for OpenCode skill packs.  
Per-worker OpenCode config often lives at `~/.local/share/<agent-id>/opencode.json` (template-isolated).

After editing: **restart OpenCode** so MCP reloads.

### Codex — `~/.codex/config.toml`

Realm entries are under `[mcp_servers.*]`:

| Section | Purpose |
|---------|---------|
| `[mcp_servers.realm_agent_launcher]` | Start/stop local workers |
| `[mcp_servers.realm_collaborator]` | Chain / council |

**Gap to fix if you want full mesh tools from Codex:** add a main Realm bridge, e.g.:

```toml
[mcp_servers.realm]
command = "/opt/homebrew/bin/python3.11"
args = ["/Users/a.developer/Documents/Realm/mcp-server/realm-mcp.py"]
startup_timeout_sec = 120

[mcp_servers.realm.env]
MCP_TRANSPORT = "stdio"
REALM_NATS_URL = "nats://agentnet_secret_token@localhost:4222"
```

(Today Codex may only have launcher + collaborator — without `realm` you lack `delegate_task` / `agent_status` / etc.)

Headless worker brain: **`codex exec`**, not interactive TUI.  
Worker env template: `services/agent-template/env.cli.example` (`REALM_RUNTIME=codex`).

After editing: **restart Codex** / new session.

### Grok — `~/.grok/config.toml`

- CLI/UI prefs: `~/.grok/config.toml`  
- Auth: `~/.grok/auth.json` (do not commit)  
- Skills: `~/.grok/skills/`  
- Bundled roles/personas: `~/.grok/bundled/roles/`, `~/.grok/bundled/personas/`  
- Sessions: `~/.grok/sessions/`

Wire Realm MCP for Grok the same way as other MCP clients (stdio → `realm-mcp.py` + `REALM_NATS_URL`). Exact key names depend on Grok’s MCP registration UI/`mcp` config for this build — keep the **script path + env** identical to OpenCode’s `realm` entry.

Headless worker brain: **`grok -p "…"`** or multi-turn agent flags with `--always-approve`.  
Worker env: `REALM_RUNTIME=grok` in `env.cli.example`.

After editing: **restart Grok** / new session.

### Workspace vs agent home (launcher)

| | Meaning | Shared? |
|--|---------|---------|
| **workspace** | Project dir the brain edits (`OPENCODE_DIR` / `REALM_WORKDIR`) | Only if intentional |
| **agent home** | Launcher private dir (`…/agent-launcher/agents/<id>/`) | Never — one per agent |

**Not clean:** five workers all with `workspace=/same/repo` writing at once (old realm-worker-a…e).  
**Clean:** one writer per tree; parallel work → **git worktrees**; reviewer may share read-only.

Launcher MCP: `launch_opencode_agent(agent_id, workspace=…)`.

### Worker homes (not CLI global config)

| Runtime | Identity env | Launcher |
|---------|--------------|----------|
| OpenCode | `~/.local/share/<id>/.env` + optional `opencode.json` | `start-opencode-agent.sh` |
| Codex / Grok | `~/.local/share/<id>/.env` (`REALM_RUNTIME=…`) | `start-cli-agent.sh` |

Templates: `services/agent-template/env.example`, `env.cli.example`, [README](services/agent-template/README.md).

### Quick “where do I change skills?”

| I want to… | Edit |
|------------|------|
| Give **OpenCode** Realm tools | `~/.config/opencode/opencode.json` → `mcp.realm*` |
| Give **Codex** Realm tools | `~/.codex/config.toml` → `[mcp_servers.realm*]` |
| Give **Grok** Realm tools | Grok MCP config + same `realm-mcp.py` path/env |
| Change OpenCode skill packs | `~/.config/opencode/skills/` |
| Change Grok skill packs | `~/.grok/skills/` |
| Change mesh skill *docs* | this file + [AGENTS.md](AGENTS.md) |
| Change worker identity | `~/.local/share/<agent-id>/.env` |

---

## 1. Skill layers (do not confuse them)

```text
┌──────────────────────────────────────────────────────────┐
│  A. Company skills     (what other agents can hire me for) │
│  B. Network skills     (Realm protocol tools)              │
│  C. Runtime skills     (OpenCode / Codex / Grok brains)    │
│  D. Tool skills        (MCP / shell / git / APIs)          │
└──────────────────────────────────────────────────────────┘
```

| Layer | Example | Who uses it |
|-------|---------|-------------|
| **A. Company** | “I implement backends” | Coordinators choosing `@worker` |
| **B. Network** | `delegate_task`, `agent_status` | Coordinators + some orchestrators |
| **C. Runtime** | `codex exec`, OpenCode run | Inside a worker process |
| **D. Tool** | `read_file`, `gh`, browser | Inside the brain while working |

Realm’s job is **B + routing to A**.  
C and D stay **behind** the worker identity so the company stays forward-compatible.

---

## 2. Network skills (Realm MCP / SDK)

Use these when you are a **coordinator** (or orchestrator), usually via MCP.

### Discovery

| Skill | Tool / API | Agent use |
|-------|------------|-----------|
| Who is online? | `list_online` | Pick a live worker |
| What can they do? | `get_profile`, `search_profiles` | Match capabilities |
| What are they doing? | `agent_status` | Stand-up / unblock |

### Jobs (prefer these over chat)

| Skill | Tool / API | Agent use |
|-------|------------|-----------|
| Assign work | `delegate_task` | Create `task.assign` + `task_id` |
| Re-delegate | `delegate_task(..., parent_task_id=)` | Vertical chain |
| Live update (as worker) | `report_progress` / SDK `report_progress` | Be visible mid-job |
| Inspect one job | `task_status` | Truth of assign/progress/result |
| List jobs | `list_tasks` | By assignee / parent / status |
| Wait for done | `await_task` | With poll fallback, not forever-block |

### Chat (short only)

| Skill | Tool / API | Agent use |
|-------|------------|-----------|
| Quick question | `ask_text` | “STATUS?”, one-liner |
| Fire-and-forget | `send_text` | Ping, note |
| Thread audit | `get_thread_messages`, `list_threads` | Context recovery |

### Infra MCP (not company employees)

| Skill | MCP server | Agent use |
|-------|------------|-----------|
| Spawn/stop local workers | `realm-agent-launcher` | Ops on **this** machine |
| Chain / council | `realm-collaborator` | Multi-agent patterns |

**Rule:** MCP harness identities (`@medusa-bridge`) are **skill surfaces for a human/CLI session**, not specialists you hire for coding jobs.

---

## 3. Company skills (advertise on your profile)

When you **register**, set `capabilities` + `metadata` so others can hire you.

### Suggested capability tags

| Capability | Meaning |
|------------|---------|
| `llm` | Can reason / write |
| `coding-agent` | Can edit code / run tools |
| `opencode-headless` | OpenCode-backed worker |
| `codex-cli` | Codex `exec` worker |
| `grok-cli` | Grok headless worker |
| `review` | Code review specialist |
| `research` | Docs / web research |
| `human-gateway` | Bridges a human chat |
| `mcp-bridge` | Coordinator tool bridge |
| `agent-orchestrator` | Multi-agent workflows |

### Suggested metadata

```json
{
  "kind": "cli-worker | opencode-llm-agent | mcp-server | telegram-gateway",
  "runtime": "codex | grok | opencode",
  "role": "worker | orchestrator | human-gateway | mcp-harness",
  "company_visible": true,
  "skills": ["backend", "python", "review"],
  "workdir": "/path/to/default/repo"
}
```

**Agent-first rule:** put **what you offer** in capabilities/skills; put **how you run** in metadata.runtime.  
Coordinators hire `@backend`, not `@codex-on-port-9`.

---

## 4. Runtime skills (brains behind a worker)

| Runtime | Headless entry | Realm wrapper | When to use |
|---------|----------------|---------------|-------------|
| **OpenCode** | OpenCode server + run | `examples/opencode_realm_agent.py` | Rich tool loop, models via OC |
| **Codex** | **`codex exec [PROMPT]`** | `examples/cli_realm_agent.py` | Codex-native agentic coding |
| **Grok** | **`grok -p` / agent flags** | `examples/cli_realm_agent.py` | Grok-native agentic coding |

### Worker skill contract (every runtime)

| # | Skill | How |
|---|-------|-----|
| 1 | Register | `AgentSDK.start()` → appear in `list_online` |
| 2 | Accept jobs | Handle `task.assign` |
| 3 | Show work | `report_progress` (ack / working / tool / text) |
| 4 | Finish | One of `task.result` / `blocked` / `failed` |
| 5 | Optional status | Answer `STATE` / short `ask_text` |

Only the “run brain” step differs. That is forward compatibility.

---

## 5. Coordinator skill recipes (agent-first)

### Hire a specialist

```text
1. list_online → filter role=worker, company_visible=true
2. agent_status @name → idle?
3. delegate_task(@name, text, title=...)
4. keep task_id
5. task_status(task_id) until terminal
```

### Vertical handoff (you are mid-tier)

```text
You hold parent task_id = T1
delegate_task(@junior, text, parent_task_id=T1) → T2
list_tasks(parent_task_id=T1) → children
agent_status(@junior) → live line
```

### Parallel council

```text
Same parent P
  delegate_task @a parent=P
  delegate_task @b parent=P
  delegate_task @c parent=P
await all terminal → synthesize
```

(Collaborator MCP automates chain/council if available.)

---

## 6. Skills you should NOT use for long work

| Anti-skill | Why |
|------------|-----|
| Long `ask_text` as a job | Timeouts; no progress model |
| Treating `@medusa-bridge` as a coder | Harness, not employee |
| Silent work (no progress) | Looks dead; retries explode |
| Multiple terminals for one `task_id` | Breaks await/status |
| Starting a second hub “to be safe” | Splits the company |

---

## 7. Skill ownership matrix

| Skill need | Prefer |
|------------|--------|
| “Who is free?” | Network: `list_online` / `agent_status` |
| “Implement this PR” | Company worker: `delegate_task` |
| “Run shell/tests” | Runtime tools **inside** the worker |
| “Start a new agent process” | Launcher MCP **on that machine** |
| “Talk to a human” | `telegram-gateway` / human channel |
| “What can the network do?” | This file + `AGENTS.md` |

---

## 8. Adding a new skill to the company

**As a new worker skill (e.g. “security review”):**

1. Create identity `@security` with capabilities `["llm","coding-agent","review","security"]`
2. Attach a runtime (Codex/Grok/OpenCode)
3. System prompt = security reviewer contract
4. Register on hub NATS
5. Coordinators: `search_profiles query=security` or hardcode `@security`

**As a new network skill (new MCP tool):**

1. Add tool to `realm-mcp.py` (or new MCP)
2. Document here + in `AGENTS.md`
3. Restart MCP clients
4. Prefer registry/shared state over private-only ledgers

**As a new runtime:**

1. Implement the 5-point worker contract in a wrapper
2. Headless CLI or API only (no interactive TUI requirement)
3. Document binary + env in agent-template + this file

---

## 9. Quick reference — “I need to…”

| I need to… | Skill / config |
|------------|----------------|
| See the team | `list_online` (via `realm` MCP) |
| See one agent’s work | `agent_status @x` |
| Give someone work | `delegate_task` |
| Show I’m working | `report_progress` |
| Check a job | `task_status` |
| Wait for a job | `await_task` + poll |
| Configure OpenCode Realm MCP | `~/.config/opencode/opencode.json` |
| Configure Codex Realm MCP | `~/.codex/config.toml` |
| Configure Grok | `~/.grok/config.toml` + `~/.grok/skills/` |
| Join as Codex worker | `cli_realm_agent` + `REALM_RUNTIME=codex` + `env.cli.example` |
| Join as Grok worker | `cli_realm_agent` + `REALM_RUNTIME=grok` + `env.cli.example` |
| Join as OpenCode worker | `opencode_realm_agent` + `start-opencode-agent.sh` |
| Repo handoff | [agent.md](agent.md) |
| Mesh law | [AGENTS.md](AGENTS.md) |

---

## 10. Version

Aligned with Realm **0.1.x**: jobs, progress, parent tasks, roster roles, multi-runtime CLI workers.

When you add a skill, update **this file** so the next agent does not rediscover it by pain.
