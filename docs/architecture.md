# Realm OS architecture

**Product:** a **job OS for agents working for you** — permanent processes with
durable identities, delegated work, live progress, and shared registry truth.

**Network / NATS:** the **kernel bus only**. The product is not “a NATS demo”;
NATS is how processes, registry, and drivers exchange messages.

**Python package:** `agentnet` (implementation of the kernel SDK/node). Package
name is not the product name; do not rename it casually.

| Doc | Purpose |
|-----|---------|
| [process-contract.md](process-contract.md) | **Freeze** identity + job + delivery contract |
| [http-gateway.md](http-gateway.md) | FastAPI / external clients |
| [../AGENTS.md](../AGENTS.md) | How mesh agents operate day to day |

---

## 1. OS concept map

```text
┌─────────────────────────────────────────────────────────────┐
│  apps (userland)                                            │
│  FastAPI HTTP gateway · Telegram gateway · company UIs      │
├─────────────────────────────────────────────────────────────┤
│  processes                                                  │
│  @workers · coordinators · human-gateway · harness sessions │
├─────────────────────────────────────────────────────────────┤
│  drivers                                                    │
│  MCP bridges · allowlisted side effects           │
├─────────────────────────────────────────────────────────────┤
│  registry (process table)                                   │
│  presence · profiles · threads · task snapshots · Postgres  │
├─────────────────────────────────────────────────────────────┤
│  kernel                                                     │
│  src/agentnet (node, sdk, task_protocol, subjects, schema)  │
│  NATS subjects as bus                                       │
├─────────────────────────────────────────────────────────────┤
│  init / boot                                                │
│  boot/docker-compose.yml · launchers · realm.sh shell       │
└─────────────────────────────────────────────────────────────┘

```

| OS idea | Realm piece | Paths |
|---------|-------------|--------|
| **Kernel** | Messaging node, envelopes, task helpers, subject names | `src/agentnet/` (`node.py`, `sdk.py`, `task_protocol.py`, `subjects.py`, `schema.py`) |
| **Bus** | NATS | `boot/docker-compose.yml`, `REALM_NATS_URL` |
| **Process table** | Registry + Postgres | `services/registry/main.py`, registry client in `src/agentnet/registry.py` |
| **Drivers** | MCP servers + mesh tools | `drivers/mcp/`; `mcp-server/` stubs |
| **Processes** | Agents / workers | `examples/*_realm_agent.py`, `services/agent-template/` |
| **Init** | Compose + agent homes / launcher | `boot/`, `services/agent-template/`, `drivers/mcp/realm-agent-launcher.py` |
| **Userland / apps** | HTTP gateway, Telegram, demos | `apps/gateway/`, `apps/demo/`, `src/agentnet/telegram_gateway.py` |
| **Shell** | Ops CLI | `boot/realm.sh`, `boot/network.sh`, `python -m agentnet` |

---

## 2. What the product is (and is not)

### Is

- A **job OS**: `task.assign → task.progress → terminal` for work that matters
- **Permanent specialists** with `@username` / `account_id`
- Horizontal and vertical **delegation** (`parent_task_id`)
- Live visibility (`agent_status`, progress text on task snapshots)
- **Forward-compatible brains** behind one process contract

### Is not

- Chat-only multiplayer (chat exists; jobs are the core)
- An LLM product that owns models (models are adapters)
- Browser-facing NATS (external clients use HTTP apps — [http-gateway.md](http-gateway.md))
- A second codebase per company (see dogfood below)

---

## 3. Layers in practice

### Kernel

Stable libraries every process links:

- Connect, register, heartbeat
- Send / request to account inbox or capability subject
- Task payload builders (`task_protocol`)
- Thread fields on messages for audit

Kernel stays thin: **transport + protocol + SDK**, not business logic.

### Registry (process table)

- Who is online (dedupe by account; `session_count`)
- Profiles, capabilities, roles, `company_visible`
- Thread message history
- Task events derived from the message stream → snapshots for `task_status` / `list_tasks`

Shared truth for coordinators and CLIs. Rebuild the registry image when
`services/registry/main.py` changes.

### Drivers

Anything that performs **side effects** or bridges external runtimes:

- `drivers/mcp/realm-mcp.py` — mesh tools for coding agents
- `realm-agent-launcher`, `realm-collaborator`
- domain helpers live outside the kernel (app-specific)

Drivers are allowlisted per process or per company distro. Processes should not
open free SQL or arbitrary production HTTP as their default work path.

### Processes

Running agents that honor [process-contract.md](process-contract.md):

- Register identity + capabilities
- Handle `task.assign`
- Emit progress; finish once
- Optional short chat / STATE

Brains (OpenCode, Codex, Grok, rules, human) are **plugged in**, not forked OS.

### Apps

Services outside the pure mesh role:

- **HTTP gateway** — REST + API keys → server-side `AgentSDK.delegate_task`
- **Telegram gateway** — human joins as a mesh process
- Company-specific demos / shop floors

Apps **publish work** onto the OS; they do not reimplement the job lifecycle
inside the HTTP process.

### Boot / shell

```bash
docker compose -f boot/docker-compose.yml up -d   # bus + registry + db
./network.sh list
./network.sh status @username
./network.sh tasks --limit 20
```

---

## 4. Three concerns on one bus

```text
1. Transport     account.{id}.inbox · agent.capability.{cap} · registry.*
2. Conversation  thread_id on messages (history / audit)
3. Jobs          task_id + task.* payloads
```

Do not collapse these: threads are not addresses; NATS subjects are not the
product API for browsers; jobs are not “long ask_text.”

---

## 5. Dogfood = reference distro

**Dogfood is not a second product.**

“Agents working for us” runs on the **same Realm OS**:

| Layer | Shared OS | Company image (distro) |
|-------|-----------|-------------------------|
| Kernel + bus | `src/agentnet`, NATS | same |
| Process contract | freeze doc | same |
| Drivers | base MCP | private tools, policies, allowlists |
| Processes | contract + adapters | our `@agents`, prompts, workspaces |
| Apps | gateway patterns | our routes, API keys, Telegram bot |

Private configs, drivers, and policies are a **company image**, not a fork of
the kernel. Features that belong in the OS graduate into kernel/docs; one-off
ops stay in the distro.

Experiments (nba, olist, CUAD artifacts, etc.) are **apps or data**, not kernel.

---

## 6. Mental model (runtime)

```text
Coordinator (Codex/Grok/OC + realm MCP  or  HTTP app + AgentSDK)
        │  delegate_task / agent_status
        ▼
     NATS hub  ←── workers register (hello + account inbox)
        │
   registry service ── Postgres (sessions, messages, task events)
        │
 Workers: OpenCode wrapper | cli_realm_agent (codex exec | grok) | humans
        │
 Drivers: MCP tools allowlist for side effects
```

---

## 7. Stability boundaries

**Freeze (see process-contract.md)**

- Identity fields and inbox/capability delivery
- `task.*` lifecycle and “finish once”
- Discovery + status surfaces
- Tool/MCP gate for side effects

**May change**

- Brain implementations and model vendors
- Registry indexing details (as long as task_status keeps working)
- HTTP route shapes in apps
- Layout renames (`drivers/`, `apps/`, `boot/`) without breaking package imports

**Do not**

- Rename `agentnet` package without a migration plan
- Make `ask_text` the job system again
- Expose NATS to browsers
- Treat OpenCode session ids as the only resume key for jobs
