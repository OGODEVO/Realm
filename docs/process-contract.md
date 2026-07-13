# Process / job contract (freeze)

**Status:** freeze-level surface for forward-compatible agents.  
**Product name:** Realm OS. **Python package:** `agentnet` (do not rename).

This document freezes what a process (agent) is, how jobs finish, and what may
change under the hood. Anything not listed here may evolve.

| Also read | Purpose |
|-----------|---------|
| [architecture.md](architecture.md) | OS layers (kernel / drivers / processes / apps) |
| [http-gateway.md](http-gateway.md) | External HTTP integration |
| [../AGENTS.md](../AGENTS.md) | Mesh operating guide for workers on the network |
| [../ORCHESTRATION.md](../ORCHESTRATION.md) | parent_task_id, pipeline, parallel patterns |

---

## 1. Why freeze this

**Forward-compat = freeze contract, swap brains.**

Durable agents outlive any single model, CLI, or IDE session. The OS promises:

- the same **identity** and **mailbox**
- the same **job lifecycle** (`task.*`)
- the same **delivery** rules (NATS subjects)
- the same **side-effect gate** (tools / MCP allowlist)

OpenCode, `codex exec`, Grok headless, rules bots, humans on Telegram, and cron
are **adapters** behind that contract — not alternate products.

---

## 2. Identity (process identity)

Every process on the mesh has a durable identity:

| Field | Meaning | Source of truth |
|-------|---------|-----------------|
| `@username` | Human-readable handle | Register / hello → registry |
| `account_id` | Durable mailbox id (`acct_…`) | Registry resolve |
| `capabilities` | Skills / routing tags | Profile + capability subjects |
| `role` | Roster class (`worker`, `coding-agent`, `human-gateway`, `mcp-harness`, `orchestrator`, `other`) | Registry metadata |
| `company_visible` | Prefer for real delegation | Registry metadata |
| `session_count` | Concurrent processes sharing the identity | Registry presence |

**Rules**

1. One **logical identity** per specialist employee (`@sandra`, not a new name per session).
2. Multiple sessions may share an identity (`session_count > 1`); that is presence, not a second company.
3. Prefer **company_visible workers** for jobs. MCP harnesses (`role=mcp-harness`) are tool bridges, not coworkers.
4. Discovery: `list_online`, `get_profile`, `search_profiles`, `agent_status` (see [AGENTS.md](../AGENTS.md)).

Code: `src/agentnet/schema.py` (`AgentInfo`), `src/agentnet/registry.py`, `services/registry/main.py`.

---

## 3. Jobs (process work unit)

A **job** is the unit of work. Chat is not a job.

### Lifecycle (must finish once)

```text
task.assign  →  task.progress*  →  task.result | task.blocked | task.failed
```

| Event | Type string | Who | Once? |
|-------|-------------|-----|-------|
| Assign | `task.assign` | Coordinator | Start of job |
| Progress | `task.progress` | Worker | Many times |
| Result | `task.result` | Worker | **One** terminal |
| Blocked | `task.blocked` | Worker | **One** terminal |
| Failed | `task.failed` | Worker | **One** terminal |

Terminal set: `TERMINAL_TASK_TYPES` in `src/agentnet/task_protocol.py`.

### Required fields

**Assign** (`build_task_assign`):

- `type`, `task_id`, `text`, `created_at`, `metadata`
- optional: `coordinator`, `title`, `parent_task_id`

**Progress** (`build_task_progress`):

- `type`, `task_id`, `text`, `event_at`, `metadata`
- optional in metadata / helpers: `percent`, `phase` (`ack` / `working` / `tool` / `text` / `status`)

**Terminal** (`build_task_result` with `status`):

- `type` ∈ {`task.result`, `task.blocked`, `task.failed`}, `task_id`, `status`, `text`, `finished_at`, `metadata`

### Coordinator path

```text
delegate_task(to, text, title=..., parent_task_id=?)  →  task_id
loop: task_status(task_id) / agent_status(@worker) until terminal
```

SDK: `AgentSDK.delegate_task`, `AgentSDK.report_progress` in `src/agentnet/sdk.py`.  
MCP: `delegate_task`, `report_progress`, `await_task`, `task_status`, `list_tasks` in `drivers/mcp/realm-mcp.py`.

### Worker path

```text
1. Receive task.assign (decode via task_protocol)
2. Emit progress phase=ack
3. Emit progress phase=working
4. Stream progress on meaningful steps
5. Emit ONE terminal: result | blocked | failed
```

### Vertical re-delegation

Child jobs set `parent_task_id` to the parent `task_id`. Anyone can then
`list_tasks(parent_task_id=…)` or inspect children via `agent_status`.

### Stability rules

1. **Always keep `task_id`.** Status and progress attach only through it.
2. **Delivery ACK ≠ job done.** Slow or missing ACKs must not redefine completion.
3. **Registry is shared truth** for task snapshots after assign/progress/result (derived from message stream).
4. **Finish once.** No second terminal that rewrites the job without a new assign.
5. **Idempotency:** reuse `task_id` / result keys on retries; do not invent a second job for the same work.

Code: `src/agentnet/task_protocol.py`, registry task snapshots in `services/registry/main.py`.

---

## 4. Delivery (routing vs conversation)

### Transport = NATS subjects (routing)

| Subject helper | Subject | Role |
|----------------|---------|------|
| `account_inbox_subject(account_id)` | `account.{account_id}.inbox` | Primary process mailbox |
| `account_receipts_subject(account_id)` | `account.{account_id}.receipts` | Delivery receipts |
| `agent_capability_subject(capability)` | `agent.capability.{capability}` | Fan-out / skill routing |

Defined in `src/agentnet/subjects.py`. Used by `AgentNode` / SDK send and request paths.

**Jobs today:** deliver `task.assign` (and progress/terminal payloads) to the
assignee’s **account inbox** (or capability subject when intentionally fanning
out). Prefer `@username` / `delegate_task(@agent)` for clarity in demos and ops.

### Conversation = threads (not routing)

| Concept | What it is | What it is not |
|---------|------------|----------------|
| `thread_id` | Conversation / audit log for messages | A process address |
| `task_id` | Job lifecycle key | A chat channel |
| `account_id` / capability | Where messages are **delivered** | Thread membership |

Do not route work by inventing thread-only addresses. Threads hang off messages
for history; **inboxes and capability subjects** deliver work.

Domain subjects (`biz.*`) are not required for the freeze contract. When added,
they still end as work under the same `task.*` lifecycle for agent processes.

---

## 5. Side effects (drivers only)

Processes must not own unconstrained side effects.

**Allowed**

- Tools / MCP allowlists attached to the process (or company distro)
- Realm SDK / MCP for mesh actions (`delegate_task`, discovery, status)
- Explicit app-layer services that the distro installs

**Not allowed as the job system**

- Free SQL against Postgres as “how work gets done”
- Arbitrary shell without a driver/tool gate
- Hidden HTTP to production systems with no allowlist

Side effects go through **drivers** (MCP servers and app services). The
brain decides; the OS/driver gate executes. See [architecture.md](architecture.md).

---

## 6. Brains are adapters

Same process contract, different brains:

| Adapter | Example entry | Notes |
|---------|---------------|--------|
| OpenCode | `examples/opencode_realm_agent.py` | Task-specific OC session; emit real `task.progress` |
| Codex | `examples/cli_realm_agent.py` + `codex exec` | Headless CLI brain |
| Grok | same CLI wrapper + `grok` | Headless CLI brain |
| Rules / scripted bot | thin `AgentSDK` + handlers | Deterministic process |
| Human (Telegram) | `src/agentnet/telegram_gateway.py` | Human as process on the mesh |
| Cron / scheduler | future / external | Assigns jobs; does not redefine lifecycle |

Launcher / homes: `services/agent-template/` (`start-opencode-agent.sh`,
`start-cli-agent.sh`).

**Swap brains without changing:** identity fields, delivery subjects, `task.*`
payload shapes, discovery tools, or terminal-once semantics.

---

## 7. What NOT to do

These break forward-compat and the company UX:

| Anti-pattern | Why it is frozen out |
|--------------|----------------------|
| **`ask_text` as the job system** | Timeouts, “it hung,” no registry progress tree. Chat is for short questions only. |
| **Free SQL / bypass tools** | Side effects escape the allowlist; jobs become un-auditable. |
| **OpenCode-session as only resume key** | Sessions die; `task_id` + registry snapshots are the durable job keys. OC session is an adapter detail. |
| **Progress only as free-form chat** | Registry shows `latest_progress_text: null`; coordinators go blind. |
| **No terminal event** | Coordinator waits forever; job never closes. |
| **Nested Realm MCP on every worker** | Duplicate harness sessions, flaky ACKs; workers should stay `--pure` unless the task needs mesh tools. |
| **Treating delivery ACK as completion** | ACK is transport; completion is `task.result` / blocked / failed + registry. |
| **New `@username` per run** | Breaks permanent specialist model and inbox continuity. |

---

## 8. Minimal `RealmProcess` adapter interface

Conceptual interface every brain wrapper implements. Map onto `AgentSDK` /
`AgentWrapper` + `task_protocol` today — this is not a separate package.

```text
RealmProcess
  identity:  @username, account_id, capabilities, role, metadata
  on_boot():
      connect / register (hello)
      subscribe account inbox (+ optional capability subjects)
  on_message(envelope):
      if task.assign → on_task(assign)
      else optional short chat / STATE
  on_task(assign):
      report_progress(ack)
      report_progress(working)
      run brain with tools via allowlist only
      emit progress on meaningful steps
      finish once: result | blocked | failed
  on_shutdown():
      goodbye / close
```

### Mapping to current code

| Interface piece | Implementation |
|-----------------|----------------|
| Connect + identity | `AgentSDK` / `AgentWrapper` → `AgentNode.start()` |
| Inbound | `on_message` / receive handler |
| Assign payload | `task_protocol.decode_task_payload` / `TASK_ASSIGN` |
| Progress | `AgentSDK.report_progress` → `build_task_progress` |
| Terminal | `build_task_result` + send JSON to coordinator |
| Delegate | `AgentSDK.delegate_task` → `build_task_assign` |
| Subjects | `src/agentnet/subjects.py` |

Reference workers: `examples/cli_realm_agent.py`, `examples/opencode_realm_agent.py`.

### Minimal Python sketch (illustrative)

```python
# Pseudocode — real workers use AgentSDK + task_protocol helpers.
async def on_task(sdk, assign, coordinator: str) -> None:
    task_id = assign["task_id"]
    await sdk.report_progress(coordinator, task_id, "ack", phase="ack")
    await sdk.report_progress(coordinator, task_id, "working", phase="working")
    # brain runs here; tools only via allowlist
    await sdk.send_json(
        coordinator,
        build_task_result(task_id=task_id, text="done", status="completed"),
        require_delivery_ack=False,
    )
```

---

## 9. Three layers on one bus (summary)

```text
Transport     NATS subjects: account.*.inbox, agent.capability.*, registry.*
Conversation  thread_id  (audit / history — not the routing address)
Jobs          task_id + task.assign|progress|result|blocked|failed
```

Freeze **identity + delivery + jobs + tool gate**. Swap **brains**.  
Do not fork the kernel for dogfood — company images are distros on the same OS
([architecture.md](architecture.md)).
