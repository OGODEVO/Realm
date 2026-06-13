# Realm — AgentNet

Agent-to-agent messaging over NATS. Discovery, threads, request-response, streaming, task protocol.

## Recent Commits

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
| drill-cancel-003 | PENDING | M4 needs rerun after restart |
| drill-pickup-regression-002 | PASS | Context preserved across restarts |
| drill-restart-001 | PASS | Restart with session continuity |
| drill-review-loop-001 | PARTIAL | Cross-machine review needs GH auth |
