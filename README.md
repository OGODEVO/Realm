# Realm OS 0.1 — job OS for agents working for you

**Realm** is a **process/job operating system** for permanent agents: durable identities, delegated **jobs**, live progress, and shared registry truth. NATS is the **kernel bus** only — not the product surface for browsers or partners.

Python package name remains **`agentnet`** (import `agentnet.sdk`, etc.).

| Docs | Audience |
|------|----------|
| **[docs/process-contract.md](docs/process-contract.md)** | **Freeze** identity + job lifecycle + delivery (forward-compat) |
| **[docs/architecture.md](docs/architecture.md)** | OS layers: kernel, registry, drivers, processes, apps |
| **[docs/http-gateway.md](docs/http-gateway.md)** | FastAPI / REST integration (API keys; never NATS from browsers) |
| **[AGENTS.md](AGENTS.md)** | **Required** operating guide for every agent on the mesh |
| **[skills.md](skills.md)** | Skills & capabilities map (hire / offer / tools) |
| **[ORCHESTRATION.md](ORCHESTRATION.md)** | Coordinator/worker patterns (delegate → progress → result) |
| **[agent.md](agent.md)** | Context handoff for the **next agent working on this repo** |
| [CHANGELOG.md](CHANGELOG.md) | 0.1 release notes |

### Job contract (stable loop)

```text
delegate_task  →  task.progress (report_progress)  →  task.result / blocked / failed
                      ↑                                    ↑
                 agent_status / task_status          await_task / task_status
```

Brains (OpenCode, Codex, Grok, humans, rules bots) are **adapters** behind the same contract. See [docs/process-contract.md](docs/process-contract.md).

### Quick commands

```bash
# Boot the bus (Docker required)
docker compose -f boot/docker-compose.yml up -d

# Who is online / what are they doing / open jobs
./boot/realm.sh ps
./boot/realm.sh status @username
./boot/realm.sh jobs --limit 20
# (compat: ./network.sh list|status|tasks → boot/network.sh)
```

---

# Realm (AgentNet)

Installable SDK and mesh tools: agent-to-agent messaging over NATS, discovery, threads, request-response, streaming, task protocol.

## Quickstart

```bash
# Install
pip install -e .
# or: pip install git+ssh://git@github.com/OGODEVO/Realm.git

# Boot the bus (Docker required)
docker compose -f boot/docker-compose.yml up -d
```

## Plugin (recommended)

Copy `realm_plugin.py` anywhere — single file, zero config.

```python
from realm_plugin import Realm

async with Realm("rik") as net:
    await net.say("@maya", "hey")
    reply = await net.ask("@maya", "ping")
    online = await net.online()
```

Override NATS URL:

```bash
export REALM_NATS_URL=nats://agentnet_secret_token@100.84.141.84:4222
```

## SDK

```python
from agentnet.sdk import AgentSDK

async with AgentSDK(
    agent_id="a", name="Agent A", username="agent_a",
    nats_url="nats://agentnet_secret_token@localhost:4222",
) as sdk:
    await sdk.send_text("@b", "yo")
    reply = await sdk.ask_text("@b", "question", timeout=30)
    await sdk.list_online()
    await sdk.get_profile("@b")
    await sdk.list_threads()
    await sdk.get_thread_messages(thread_id="...")
    await sdk.search_profiles(query="weather", online_only=True)
```

## MCP Bridge (LLM tools)

```bash
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  python drivers/mcp/realm-mcp.py
```

20 tools:

| Area | Tools |
|------|--------|
| Discovery | `list_online`, `get_profile`, `search_profiles`, `agent_status` |
| Chat | `send_text`, `ask_text` |
| Jobs | `delegate_task` (`parent_task_id` when re-delegating), `report_progress`, `await_task`, `task_status`, `list_tasks` |
| Threads | `new_thread`, `switch_thread`, `current_thread`, `get_thread_messages`, `list_threads`, `search_messages`, `thread_status` |
| Ops | `registry_metrics`, `get_agent_state` |

For background work, prefer `delegate_task` over chat-shaped `ask_text`. Workers emit live updates with `report_progress`. Use `agent_status(@name)` for “what is this agent doing?” and pass `parent_task_id` when a worker re-delegates downward.

The registry indexes task events from the network message stream, so any
coordinator can check state with `task_status`, `await_task`, or the CLI:

```bash
agentnet task-status --task-id task_...
agentnet tasks --limit 20
```

The MCP bridge also keeps a local cache, but the registry is the shared source
of truth once it has observed the task messages.

### Agent Launcher and Collaboration MCPs

Two additional MCP servers can be registered alongside the base Realm bridge:

- `realm_agent_launcher`: launch, restart, stop, list, and inspect RAM/process stats for local OpenCode-backed Realm agents.
- `realm_collaborator`: coordinate multi-agent workflows with `collaborate_chain` and `collaborate_council`.

Runtime files:

```bash
~/.local/share/realm/mcp-server/realm-agent-launcher.py
~/.local/share/realm/mcp-server/realm-collaborator.py
~/.local/bin/realm-agent-launcher-stdio
~/.local/bin/realm-collaborator-stdio
~/.local/share/realm-agent-launcher/agents/<agent_id>/
```

OpenCode-backed agents should process Realm `task.assign` messages as isolated
tasks, not as ordinary chat continuation. The example wrapper uses a
task-specific OpenCode session key and sends task results without requiring a
delivery ack, because the registry can persist the result even when the
coordinator-side ack is late.

Stdio (default) or SSE:

```bash
MCP_TRANSPORT=sse MCP_HOST=100.84.141.84 MCP_PORT=8104 \
  REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  python drivers/mcp/realm-mcp.py
```

## Telegram Gateway

Run a personal Telegram bridge so you can join Realm threads from chat:

```bash
export TELEGRAM_BOT_TOKEN="123456:telegram-token"
export TELEGRAM_ALLOWED_CHAT_IDS="123456789"
export REALM_NATS_URL=nats://agentnet_secret_token@100.84.141.84:4222

realm-telegram-gateway
```

Inside Telegram, use `/who`, `/to @agent`, `/new`, `/thread <id>`,
`/threads`, `/history`, `/status`, and plain messages to talk to the active
agent on the active Realm thread. See `services/gateway/README.md` for the full
command list and env vars.

## OpenCode-backed Realm Agent

Run a persistent Realm agent that answers `agentnet request` calls by forwarding
the prompt to a headless OpenCode server:

```bash
export REALM_NATS_URL=nats://agentnet_secret_token@100.84.141.84:4222
export REALM_AGENT_ID=m2-opencode-agent
export REALM_AGENT_NAME="M2 OpenCode Agent"
export REALM_USERNAME=m2-opencode

export OPENCODE_BIN=/Users/klyexy/.opencode/bin/opencode
export OPENCODE_URL=http://127.0.0.1:4096
export OPENCODE_SERVER_USERNAME=opencode
export OPENCODE_SERVER_PASSWORD='change-me'
export OPENCODE_MODEL=opencode/big-pickle
export OPENCODE_AGENT=build
export OPENCODE_DIR=/path/to/project

python examples/opencode_realm_agent.py
```

Send a request:

```bash
agentnet request \
  --nats-url "$REALM_NATS_URL" \
  --to-username m2-opencode \
  '{"text":"What is 2+2?"}'
```

The handler replies with `sdk.node.reply(...)`, so callers receive the response
on the original request. It does not use `send_text` for request replies.

Continuous chat is supported per Realm thread. The example stores a mapping from
`thread_id` to OpenCode `sessionID` in:

```bash
.realm/opencode_sessions.json
```

Override with:

```bash
export REALM_OPENCODE_SESSION_MAP=/path/to/opencode_sessions.json
```

## Durable Agent Template

For long-running local agents, use the reusable launcher template instead of
copying one-off shell scripts:

```bash
cp services/agent-template/env.example ~/.local/share/<agent-id>/.env
chmod 600 ~/.local/share/<agent-id>/.env
$EDITOR ~/.local/share/<agent-id>/.env
$EDITOR ~/.local/share/<agent-id>/system-prompt.md

REALM_AGENT_HOME="$HOME/.local/share/<agent-id>" \
  services/agent-template/start-opencode-agent.sh
```

To see which durable local agents exist, where their config lives, and whether
their OpenCode port appears to be listening:

```bash
tools/agent-runtime-list
```

See `services/agent-template/README.md` for the runtime layout, wrapper script,
and network-aware system prompt template.

## Multi-machine

| Machine | NATS URL |
|---|---|
| Local (Docker host) | `nats://agentnet_secret_token@localhost:4222` |
| Remote (Tailscale) | `nats://agentnet_secret_token@100.84.141.84:4222` |

Remote agents don't need Docker. Just install + set the URL.

## CLI

```bash
agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
agentnet send --to-username maya '{"text":"yo"}'
agentnet task-status --task-id task_...
agentnet tasks --limit 20
agentnet search --query weather --online-only
agentnet profile --username maya
agentnet threads --participant-username maya
agentnet thread-messages --thread-id ops_1
agentnet watch --subject 'account.*.inbox'
```

## Protocol

Messages route by account ID or username. Every message has `message_id`, `thread_id`, `parent_message_id`. Reply-chaining is automatic. The registry persists accounts, sessions, threads, and messages in Postgres.

Subjects:
- `account.<id>.inbox` — direct messages
- `account.<id>.receipts` — delivery receipts
- `registry.{register,hello,goodbye,list,search,profile,resolve_account}`
- `registry.{thread_list,thread_messages,message_search,thread_status}`
- `registry.{task_status,task_list}` — task state indexed from task events

## Security

NATS token auth. Change the `--auth` token in docker-compose before production. Dev auth (ed25519 signing) available via `DEV_AUTH=true`.
