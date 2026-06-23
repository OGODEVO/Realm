# Realm — AgentNet

Agent-to-agent messaging over NATS. Discovery, threads, request-response, streaming, task protocol.

## Recent Commits

`pending` **feat: MCP-backed background task delegation**
- Added typed task payloads: `task.assign`, `task.result`, `task.blocked`, `task.failed`
- Added MCP tools: `delegate_task`, `await_task`, `task_status`, `list_tasks`
- Registry now exposes `registry.task_status` and `registry.task_list`
- CLI now supports `agentnet task-status --task-id ...` and `agentnet tasks`
- Smoke test passed via MCP SSE against `@m4-coder`: `task_01kvvbpz2h56gbxnssjv4ye9jn`
- Registry task lookup handles JSON-string task payloads as well as JSON objects

`f570132` **refactor: flatten wrapper — top-level helpers, single handler**
- Extracted wrapper closures to top-level helpers
- Replaced nested dispatch with one `handle_message` path
- Removed dead timeout handling and reused `_export_text`

`9565e4d` **feat: process direct messages from teammates through OpenCode**
- Direct messages from known teammate agents now go through OpenCode
- Unified `CANCEL` and `STATE` handling for request and direct messages
- Direct replies use `send_text`

`eb18466` **docs: add cancel fix and drill status board to agent.md**
- Added cancellation fix notes
- Added drill status table

`c286c41` **drill-review-loop-001: mark PASS, both agents have GitHub MCP**
- Review loop drill is now passing
- Both agents have GitHub MCP available

`fa131f2` **drill-cancel-003: confirmed PASS on M4 after restart**
- M4 restart rerun confirmed cancellation pass

`663c212` **fix: _extract_cancel_task_id dict-payload early-return bug**
- CANCEL task_id now correctly parsed from text field when no explicit task_id key
- Drill-cancel-003 verified PASS on M2 after fix

`05cfed1` **feat: cross-machine state, cancellation, structured progress**
- STATE handler — agents reply to `STATE` requests with full JSON
- `get_agent_state` — local file fallback to network `ask_text @agent STATE`
- Cancellation: `CANCEL task_id=X` stops agents mid-work (pre-check + poll check)
- Progress messages structured: `type/subtype/text/visible_by_default/task_id`
- New states: `assigned`, `cancelling`, `cancelled`
- `tools/agent-state-update` — atomic state updater script

`659e5dc` **feat: structured state tracking + network state tool**
- Agent lifecycle writes state JSON (acknowledged → working → done/failed)
- `get_agent_state` tool in realm-mcp.py — network-readable agent status
- `agent-state-update` script for atomic JSON updates

`be458cf` **feat: live thread streaming, task protocol, no-timeout export polling**

- `examples/opencode_realm_agent.py` — rewritten `ask_opencode` with live export polling
  - Mid-task thread updates: [thinking], reasoning, and status streamed to Realm threads
  - Task protocol: ACK → WORKING → PROGRESS → DONE/FAILED with `task_id` tracking
  - No artificial timeouts — export polls until task completes
  - Supports `REALM_SYSTEM_PROMPT` and `REALM_BLOB_DIR` env vars
- `mcp-server/realm-mcp.py` — `ask_text` timeout increased to 24h

## Agents

| Agent | Location | MCP | Model |
|---|---|---|---|
| `@m4-dl` | M4 (`127.0.0.1:4196`) | realm, github | deepseek/deepseek-v4-pro |
| `@m4-coder` | M4 (`127.0.0.1:4197`) | realm, github | OpenCode default |
| `@eng-m2` | M2 (`http://100.101.117.116:4096`) | realm, medusa-tools, github, cua-driver | opencode/big-pickle |
| `@medusa-bridge` | M4 (`100.84.141.84:8104`) | MCP bridge | — |
| `@m2-opencode-mcp` | M2 | MCP bridge | — |

## Quickstart

```bash
pip install -e .
docker compose -f docker/docker-compose.yml up -d
```

## MCP Bridge (LLM tools)

```bash
MCP_TRANSPORT=sse MCP_HOST=100.84.141.84 MCP_PORT=8104 \
  REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  python mcp-server/realm-mcp.py
```

## Headless Agent

```bash
OPENCODE_URL=http://127.0.0.1:4196 \
  REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  REALM_AGENT_ID=m4-dl REALM_USERNAME=m4-dl \
  python examples/opencode_realm_agent.py
```

## Durable Local Agents

Reusable launcher template: `services/agent-template/start-opencode-agent.sh`

Inventory command:

```bash
tools/agent-runtime-list
```

Current local runtime homes:

| Agent | Home | Port | Prompt | Logs |
|---|---|---:|---|---|
| `@m4-dl` | `/Users/a.developer/.local/share/m4-dl` | 4196 | `system-prompt.md` | `/tmp/m4-dl-realm.log`, `/tmp/m4-dl-opencode.log` |
| `@m4-coder` | `/Users/a.developer/.local/share/m4-coder` | 4197 | `system-prompt.md` | `/tmp/m4-coder-realm.log`, `/tmp/m4-coder-opencode.log` |

Each durable agent keeps identity, port, NATS URL, model/server settings, and
private tokens in `~/.local/share/<agent-id>/.env`. Keep long-lived role and
network instructions in `~/.local/share/<agent-id>/system-prompt.md`.

## Task Protocol

| State | Format | When |
|---|---|---|
| ACK | `ACK task_id=X: received` | Immediate |
| WORKING | `WORKING task_id=X: [summary]` | LLM start |
| PROGRESS | `{"type":"progress","subtype":"...","text":"...","visible_by_default":false}` | Auto-streamed |
| DONE | Reply with `task_id` | Complete |
| FAILED | Reply with error + `task_id` | Error |
| CANCELLED | `CANCEL task_id=X` received, work stopped | Cancelled |

Cancellation via state file + direct/reply message. Agents check state pre-work and during processing.

## Cross-Machine State

Ask any agent: `STATE` → returns current state JSON. `get_agent_state --agent <name>` tries local file first, falls back to network `STATE` request.

## Network Skill

`.opencode/skills/network/SKILL.md` — teaches agents team discovery, delegation, and protocol.

## State Tracking

Every agent writes its current task state to a local JSON file:
`~/.local/share/<agent>/state/<agent>.json`

m4-dl state: `/Users/a.developer/.local/share/m4-dl/state/m4-dl.json`

Network tool: `get_agent_state --agent m4-dl` — returns the state file as JSON, readable by any agent on the network.

## Multi-Machine

| Machine | NATS URL |
|---|---|
| Local | `nats://agentnet_secret_token@localhost:4222` |
| Tailscale | `nats://agentnet_secret_token@100.84.141.84:4222` |

## Drill Status

| Drill | Result | Notes |
|---|---|---|
| drill-discovery-001 | PASS | Both agents discover each other |
| drill-pickup-001 | PASS | Task pickup + ACK/WORKING/DONE |
| drill-state-001 | PASS | Cross-machine state lookup works |
| drill-cancel-001 | FIXED | Task_id parsing bug patched (663c212) |
| drill-cancel-003 | PASS | Confirmed on M2 and M4 after restart |
| drill-pickup-regression-002 | PASS | Context preserved across restarts |
| drill-restart-001 | PASS | Restart with session continuity |
| drill-review-loop-001 | PASS | Both agents have GitHub MCP |
