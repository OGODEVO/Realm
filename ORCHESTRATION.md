# Agent Orchestration Protocol (Sandra / Daniela)

This is the standard way permanent agents work together on Realm.

## Mental model

Think of agents as **employees with durable identities**, not temporary chat sessions.

| Object | Meaning |
|--------|---------|
| **Identity** | `@sandra`, `@daniela` — stable handle + account mailbox |
| **Job** | `task.assign` with `task_id` — unit of work (not chat) |
| **Parent job** | `parent_task_id` — vertical re-delegation chain |
| **Progress** | `task.progress` — live “what I am doing” |
| **Terminal** | `task.result` / `task.blocked` / `task.failed` |
| **Visibility** | `agent_status(@name)` — presence + active jobs + latest progress |

## Chat vs job

| Intent | Use |
|--------|-----|
| Short question / stand-up chat | `ask_text` / `send_text` |
| Real work that may take minutes | `delegate_task` |
| “What is X doing?” | `agent_status` (preferred) or `list_tasks` |
| Worker live updates | `report_progress` |

Never hang a coordinator on `ask_text` for multi-minute coding work.

## Vertical delegation (Boss → Sandra → Daniela)

```text
Boss                    Sandra                      Daniela
  |                        |                            |
  |-- delegate_task ------>|                            |
  |   task_id=T1           |                            |
  |                        |-- delegate_task ---------->|
  |                        |   task_id=T2               |
  |                        |   parent_task_id=T1        |
  |                        |                            |-- report_progress -->
  |                        |                            |   (on T2)
  |-- agent_status @daniela ----------------------------------------------->
  |   "Daniela is working on X: latest progress..."
  |-- agent_status @sandra ----------------------------------------------->
  |   "Sandra is working..." + children via parent_task_id
```

### SDK

```python
# Boss → Sandra
r1 = await boss.delegate_task("@sandra", "Ship the API fix", title="api-fix")
sandra_task = r1.trace_id  # task_id

# Sandra → Daniela (child)
r2 = await sandra.delegate_task(
    "@daniela",
    "Implement the endpoint",
    title="implement-endpoint",
    parent_task_id=sandra_task,
)
daniela_task = r2.trace_id

# Daniela reports while working (to Sandra / thread peers)
await daniela.report_progress(
    "@sandra",
    daniela_task,
    "Writing handler and tests",
    percent=40,
    phase="coding",
)

# Anyone: what is Daniela doing?
status = await boss.agent_status("@daniela")
print(status["summary"])
# "@daniela is working on implement-endpoint: Writing handler and tests"

# List children of Sandra's job
children = await boss.list_tasks(parent_task_id=sandra_task)
```

### MCP tools

- `delegate_task(to, text, parent_task_id=...)`
- `report_progress(to, task_id, text, percent=, phase=)`
- `agent_status(target)`
- `list_tasks(assignee=, coordinator=, parent_task_id=, status=)`
- `task_status(task_id)` / `await_task(task_id)`

### CLI

```bash
./network.sh list
./network.sh status @daniela
./network.sh tasks --parent task_...
PYTHONPATH=src python -m agentnet agent-status @sandra
```

## Horizontal peer work

Peers can:

1. **Discover**: `list_online` / `search_profiles`
2. **Ask status**: `agent_status("@peer")` without interrupting them
3. **Chat briefly**: `ask_text` for a quick question
4. **Delegate sideways**: `delegate_task` with optional `parent_task_id` of your own job

## Pipeline (line: 1 → 2 → 3)

Each step completes, then the next is assigned with `parent_task_id` linking the chain:

```text
task A (reviewer)  parent=null
task B (coder)     parent=A   after A needs implementation
task C (tester)    parent=B   after B ships code
```

Or sequential handoff inside one coordinator that only advances on terminal status.

## Parallel (fan-out / council)

One parent job, many children with the **same** `parent_task_id`:

```text
parent P
  ├─ child to @daniela  parent_task_id=P
  ├─ child to @marco    parent_task_id=P
  └─ child to @lena     parent_task_id=P
```

Coordinator:

```python
await coordinator.list_tasks(parent_task_id=P)
```

Collect until all children are terminal, then synthesize.

## What agents should care about when they get a job

1. Is this a **job** (`task.assign`) or **chat**?
2. **ACK receipt** ≠ finished
3. **Claim** the work (only one runtime should process; avoid multi-session double work)
4. Emit **progress** regularly
5. Emit **one terminal** result: completed | blocked | failed
6. Requesters may disconnect; the job still completes via registry task state

## Presence note

`list_online` returns **one row per logical identity** (`account_id`), with `metadata.session_count` when multiple MCP/client sessions are attached. Prefer durable workers (`@daniela`) over harness clones (`medusa-bridge` × N).
