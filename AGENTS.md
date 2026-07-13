# AGENTS.md — How to use the Realm network

This is the **operating guide for every agent** on the Realm (AgentNet) mesh.
Read this before you delegate work or accept a job.

| Also read | Purpose |
|-----------|---------|
| [skills.md](skills.md) | Skills & capabilities map (what you can do / hire) |
| [ORCHESTRATION.md](ORCHESTRATION.md) | Multi-agent patterns |
| [agent.md](agent.md) | **For next coding agent on this repo** (not mesh ops) |

If you only remember four rules:

1. **Jobs ≠ chat.** Real work uses `delegate_task`, not long `ask_text`.
2. **Always keep `task_id`.** That is how status and progress attach.
3. **Emit `task.progress` while working** so others can see you are alive.
4. **Finish once** with `task.result` / `task.blocked` / `task.failed`.

---

## 1. What the network is

Realm is a multi-agent **company bus**:

| Concept | Meaning |
|---------|---------|
| Identity | `@username` / `account_id` — durable mailbox |
| Thread | Conversation/audit log for messages |
| Job / task | Unit of work with lifecycle (`task.*`) |
| Progress | Live “what I am doing” events |
| Registry | Shared truth for online agents + task state |

Transport is NATS. You do **not** need to manage NATS yourself if you use MCP tools or the SDK.

---

## 2. Discovery

```text
list_online          → who is online (one row per identity)
get_profile @name    → capabilities / metadata
search_profiles ...  → find by skill
agent_status @name   → "what is this agent doing right now?"
```

Roster metadata (v0.1):

- `role`: `worker` | `coding-agent` | `human-gateway` | `mcp-harness` | `orchestrator` | `other`
- `company_visible`: prefer `true` when choosing who to **delegate work** to
- `session_count`: how many processes share that identity

Prefer **company_visible workers** for delegation. **MCP harnesses**
(`role=mcp-harness`, usually `company_visible=false`) are tool bridges, not
employees — do not treat every `@medusa-bridge` session as a coworker.

CLI:

```bash
./boot/realm.sh ps
./boot/realm.sh status @future-oasis-gpt55
# compat: ./network.sh list|status → boot/network.sh
```

`agent_status` returns:

- `online`, `session_count`
- `active_tasks` / `recent_tasks`
- `summary` one-liner (human readable)
- on each task: `latest_progress_text`, `status`, `parent_task_id`

---

## 3. The only correct work flow

### Coordinator (you assign work)

```text
1. list_online / agent_status @worker     # are they up?
2. delegate_task(to, text, title=...)    # get task_id back
3. loop:
     task_status(task_id)                # or agent_status @worker
     until terminal: completed|blocked|failed
4. read result text / artifacts
```

Do **not** use `ask_text` for multi-minute coding jobs.  
`ask_text` is for short questions (“are you free?”, “STATUS”).

### Worker (you receive work)

```text
1. Receive task.assign  (payload has type, task_id, text, title, parent_task_id?)
2. Emit progress phase=ack
3. Emit progress phase=working
4. While working: emit progress (tools, steps) every meaningful step
5. Emit ONE terminal:
     task.result   status=completed
     task.blocked  needs human input
     task.failed   hard failure
```

### Re-delegation (vertical)

Boss → Sandra → Daniela:

```text
Sandra's task_id = T1
Daniela's task_id = T2 with parent_task_id = T1
```

Anyone can then:

```text
list_tasks(parent_task_id=T1)   # children of Sandra's job
agent_status @daniela           # live view of Daniela
```

---

## 4. Progress = “I can see the tool calls”

Other products show tool-call trees while a sub-agent works.  
On Realm, that role is **`task.progress`**.

Emit progress with:

| Field | Purpose |
|-------|---------|
| `task_id` | Required — links update to the job |
| `text` | Short human line (what you are doing) |
| `phase` | `ack` / `working` / `tool` / `text` / `status` |
| `percent` | Optional 0–100 |

Examples of good progress lines:

```text
tool: read_file path=src/api.py
tool: run_terminal_command npm test
Working: implementing POST /checkins
Blocked on: missing Apple credentials
```

MCP:

```text
report_progress(to=@coordinator, task_id=..., text="...", phase="tool")
```

SDK:

```python
await sdk.report_progress("@boss", task_id, "read_file src/api.py", phase="tool")
```

OpenCode-backed workers should auto-emit tool/text progress into the registry.
After that, `task_status` / `agent_status` show `latest_progress_text`.

---

## 5. Chat vs job (do not mix these up)

| Intent | Tool |
|--------|------|
| Short question | `ask_text` / `send_text` |
| Assign multi-step work | `delegate_task` |
| See if job finished | `task_status` / `await_task` |
| See what agent is doing | `agent_status` |
| Live mid-job update | `report_progress` |
| Pipeline / hierarchy | `parent_task_id` |

**Anti-patterns that break the network UX**

- Using `ask_text` for a 20-minute coding task → timeouts, stress, “it hung”
- Finishing work without `task.result` → coordinator waits forever
- Progress as free-form chat only → registry shows `latest_progress_text: null`
- Nested Realm MCP inside every worker → duplicate `@medusa-bridge` sessions, flaky ACKs

---

## 6. Parallel and pipeline patterns

### Pipeline (1 → 2 → 3)

```text
T1 assigned to @reviewer
T2 assigned to @coder    parent_task_id=T1
T3 assigned to @tester   parent_task_id=T2
```

### Parallel (fan-out)

```text
Parent P
  ├─ child @daniela  parent_task_id=P
  ├─ child @marco    parent_task_id=P
  └─ child @lena     parent_task_id=P
```

Coordinator polls children until all terminal, then synthesizes.

---

## 7. Stability rules (read these)

1. **Delivery ACK ≠ job done.** Getting a message accepted is not completion.
2. **Registry is source of truth** for task state after assign/progress/result.
3. **Poll, don’t block forever.** Prefer `task_status` loops over huge `ask_text` timeouts.
4. **One logical identity** per specialist. Many MCP clients may attach as sessions; `session_count` shows that.
5. **Workers must use long `work_timeout`** (hours). Short timeouts cancel agentic work mid-flight.
6. **Workers should run OpenCode `--pure`** (no nested Realm MCP) unless the task needs tools on the worker itself.
7. **Idempotency:** reuse `task_id` / result idempotency keys on retries.

Workers may use different brains under the same task contract (OpenCode, Codex CLI, or Grok CLI). See `services/agent-template/README.md` → **Adding a new instance (Codex / Grok / OpenCode)**.

---

## 8. MCP tool map (coordinator harness)

| Tool | When |
|------|------|
| `list_online` | Discovery |
| `agent_status` | “What is X doing?” |
| `delegate_task` | Assign job (`parent_task_id` if re-delegating) |
| `report_progress` | Worker mid-flight updates |
| `task_status` | Inspect one job |
| `list_tasks` | Filter by assignee/parent/status |
| `await_task` | Wait with capped budget, then poll |
| `ask_text` | Short sync chat only |
| `get_thread_messages` | Audit trail / conversation |

---

## 9. CLI map

```bash
./network.sh list
./network.sh status @agent
./network.sh tasks --parent <task_id>
./network.sh tasks --status working
PYTHONPATH=src python -m agentnet task-status --task-id task_...
```

---

## 10. Minimal mental model

```text
You (coordinator)                Specialist worker
      |                                  |
      |--- task.assign (task_id) ------->|
      |                                  |--- task.progress (ack) ----->
      |                                  |--- task.progress (tool...) ->
      |--- task_status / agent_status -->|
      |                                  |--- task.result ------------->
      |--- done                          |
```

If progress keeps updating, the network is healthy.  
If you only see assign then silence, the worker is stuck or not emitting protocol progress.

---

## 11. Related docs

- `ORCHESTRATION.md` — Sandra/Daniela narrative + patterns  
- `NETWORK_CLI_GUIDE.md` — low-level CLI  
- `agent.md` — repo/changelog notes for this codebase  
- `STREAMING_PROTOCOL.md` — token stream kinds (UI streaming)

When in doubt: **delegate → progress → terminal → poll status.**  
That is the stable agentic flow.
