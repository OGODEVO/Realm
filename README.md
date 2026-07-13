# Realm

**Event-driven job mesh for agents.**

Realm is a network layer so permanent agents can:

- have durable identities (`@username` / account id)
- take **jobs** (not endless chat)
- report **progress** and terminal status
- be discovered and coordinated across machines

NATS is the bus. The product is the **job contract** and the mesh around it.

Python package: **`agentnet`** (`from agentnet.sdk import AgentSDK`).

---

## Job contract

```text
delegate_task  →  report_progress  →  completed | blocked | failed
       │                  │                    │
   task_id          agent_status          task_status
```

Use **jobs** for multi-step work. Use **chat** only for short Q&A.

| Docs | What |
|------|------|
| [docs/process-contract.md](docs/process-contract.md) | Identity + job lifecycle (stable) |
| [docs/architecture.md](docs/architecture.md) | Kernel / bus / registry / apps |
| [docs/http-gateway.md](docs/http-gateway.md) | REST → jobs (no NATS in browsers) |
| [ORCHESTRATION.md](ORCHESTRATION.md) | Coordinator / worker patterns |
| [AGENTS.md](AGENTS.md) | How agents should behave on the mesh |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

---

## Repo layout (product surface)

```text
src/agentnet/     # kernel: SDK, node, tasks, registry client
services/         # registry (process table), agent-template
drivers/mcp/      # MCP bridges (jobs, discovery, launcher, collaborator)
mcp-server/       # thin stubs → drivers/mcp
apps/             # userland: HTTP gateway, demos
boot/             # compose + shell (network up)
examples/         # sample agents
docs/             # architecture + contracts
scripts/          # small ops helpers
tests/
ts-sdk/           # TypeScript client
```

Not product: old domain tool packs, one-off HTML apps, experiment data dumps.

---

## Quickstart

```bash
# Install
pip install -e .

# Bus (Docker)
docker compose -f boot/docker-compose.yml up -d
# or: ./boot/realm.sh  /  ./boot/network.sh
# compat: ./network.sh → boot/network.sh

export REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222
```

Change the NATS auth token before production.

### Minimal plugin

```python
from realm_plugin import Realm

async with Realm("rik") as net:
    await net.say("@maya", "hey")
    reply = await net.ask("@maya", "ping")
    online = await net.online()
```

### SDK (jobs)

```python
from agentnet.sdk import AgentSDK

async with AgentSDK(
    agent_id="desk",
    name="Desk",
    username="desk",
    nats_url="nats://agentnet_secret_token@localhost:4222",
) as sdk:
    r = await sdk.delegate_task("@worker", "do the thing", title="example")
    # r → task_id; poll with task_status / await_task
    await sdk.list_online()
    await sdk.agent_status("@worker")
```

### CLI

```bash
agentnet list
agentnet status @username          # via ./boot/realm.sh status
agentnet task-status --task-id task_...
agentnet tasks --limit 20
agentnet send --to-username maya '{"text":"yo"}'
```

```bash
./boot/realm.sh ps
./boot/realm.sh status @username
./boot/realm.sh jobs --limit 20
```

---

## HTTP gateway (apps)

External clients should not touch NATS. Use the FastAPI gateway:

```bash
# see apps/gateway/README.md
# API key → server-side AgentSDK.delegate_task
```

Demo workers: `apps/demo/` (e.g. refund agent e2e).

Docs: [docs/http-gateway.md](docs/http-gateway.md).

---

## MCP (for LLM agents)

Canonical path: **`drivers/mcp/`** (`mcp-server/` is a stub).

```bash
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  python drivers/mcp/realm-mcp.py
```

| Area | Tools |
|------|--------|
| Discovery | `list_online`, `get_profile`, `search_profiles`, `agent_status` |
| Jobs | **`delegate_task`**, **`report_progress`**, **`await_task`**, **`task_status`**, **`list_tasks`** |
| Chat | `send_text`, `ask_text` (short only) |
| Threads | `new_thread`, `get_thread_messages`, `list_threads`, … |
| Extra | `realm-agent-launcher`, `realm-collaborator` |

Prefer **`delegate_task`** over long `ask_text` for real work.

---

## OpenCode-backed agent

```bash
export REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222
export REALM_USERNAME=my-agent
export OPENCODE_URL=http://127.0.0.1:4096
export OPENCODE_DIR=/path/to/project

python examples/opencode_realm_agent.py
```

Durable layout: `services/agent-template/`.  
Ops helpers: `scripts/agent-runtime-list`, `scripts/agent-state-update`.

---

## Telegram

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_ALLOWED_CHAT_IDS=...
export REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222

realm-telegram-gateway
```

See `services/gateway/README.md`.

---

## Multi-machine

| Where | `REALM_NATS_URL` |
|-------|------------------|
| Local Docker | `nats://agentnet_secret_token@localhost:4222` |
| Tailscale host | `nats://agentnet_secret_token@<tailscale-ip>:4222` |

Remote agents only need the SDK + NATS URL (no Docker required on every machine).

---

## Protocol (short)

- Route by account id or `@username`
- Messages: `message_id`, `thread_id`, `parent_message_id`
- Job events: `task.assign` → `task.progress` → terminal result
- Registry indexes presence + tasks (shared source of truth)

Subjects include `account.<id>.inbox`, `registry.*`, `registry.task_status`, `registry.task_list`.

---

## Security

- NATS token auth (set in compose / `REALM_NATS_URL`)
- Optional `DEV_AUTH=true` (ed25519)
- Keep API keys and bot tokens in env / local `.env` (gitignored)
