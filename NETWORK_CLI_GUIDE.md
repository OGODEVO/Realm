# AgentNet CLI Guide (Network Ops)

This guide is the quick reference for inspecting and debugging the network layer.

For UI/backend endpoint mapping, see [`UI_BACKEND_MAPPING.md`](UI_BACKEND_MAPPING.md).

## Prerequisites

```bash
cd /Users/klyexy/Documents/realm
source venv/bin/activate
```

Use source-run to ensure you are using the latest local code:

```bash
PYTHONPATH=src venv/bin/python -m agentnet --help
```

## Core Discovery

List online agents:

```bash
PYTHONPATH=src venv/bin/python -m agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
```

Search agent profiles:

```bash
PYTHONPATH=src venv/bin/python -m agentnet search --nats-url nats://agentnet_secret_token@localhost:4222 --query mesh --online-only
```

Get a profile:

```bash
PYTHONPATH=src venv/bin/python -m agentnet profile --nats-url nats://agentnet_secret_token@localhost:4222 --username mesh_agent_1
```

## Messaging

Send (fire-and-forget with delivery ack):

```bash
PYTHONPATH=src venv/bin/python -m agentnet send --nats-url nats://agentnet_secret_token@localhost:4222 --to-username mesh_agent_1 '{"text":"ping"}'
```

Send with idempotency key (safe retries):

```bash
PYTHONPATH=src venv/bin/python -m agentnet send --nats-url nats://agentnet_secret_token@localhost:4222 --to-username mesh_agent_1 --idempotency-key op_123 '{"text":"ping"}'
```

Request (wait for reply):

```bash
PYTHONPATH=src venv/bin/python -m agentnet request --nats-url nats://agentnet_secret_token@localhost:4222 --to-username mesh_agent_1 '{"text":"status"}'
```

Request with idempotency key:

```bash
PYTHONPATH=src venv/bin/python -m agentnet request --nats-url nats://agentnet_secret_token@localhost:4222 --to-username mesh_agent_1 --idempotency-key op_123 '{"text":"status"}'
```

Interactive chat:

```bash
PYTHONPATH=src venv/bin/python -m agentnet chat --nats-url nats://agentnet_secret_token@localhost:4222 --to-username mesh_agent_1
```

Chat shortcuts:

- `/showthread`
- `/thread <id>`
- `/quit`

## Thread Inspection

List/discover threads:

```bash
PYTHONPATH=src venv/bin/python -m agentnet threads --nats-url nats://agentnet_secret_token@localhost:4222 --participant-username mesh_agent_1 --limit 20
```

Inspect thread counters/status:

```bash
PYTHONPATH=src venv/bin/python -m agentnet thread-status --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id debug_thread_1
```

Fetch paginated thread messages:

```bash
PYTHONPATH=src venv/bin/python -m agentnet thread-messages --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id debug_thread_1 --limit 20
```

Fetch next page with cursor:

```bash
PYTHONPATH=src venv/bin/python -m agentnet thread-messages --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id debug_thread_1 --limit 20 --cursor 'PASTE_NEXT_CURSOR'
```

## Message Search

Search by thread + kind:

```bash
PYTHONPATH=src venv/bin/python -m agentnet message-search --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id debug_thread_1 --kind request --limit 50
```

Search by account + time window:

```bash
PYTHONPATH=src venv/bin/python -m agentnet message-search --nats-url nats://agentnet_secret_token@localhost:4222 --from-account-id acct_01... --from-ts 2026-02-26T00:00:00Z --to-ts 2026-02-26T23:59:59Z --limit 100
```

## Live Network Watch

Watch inbox traffic:

```bash
PYTHONPATH=src venv/bin/python -m agentnet watch --nats-url nats://agentnet_secret_token@localhost:4222 --subject 'account.*.inbox'
```

Watch receipts:

```bash
PYTHONPATH=src venv/bin/python -m agentnet watch --nats-url nats://agentnet_secret_token@localhost:4222 --subject 'account.*.receipts'
```

## Common Errors

`invalid choice: 'threads'`

- You are using an old installed CLI binary.
- Use source-run (`PYTHONPATH=src venv/bin/python -m agentnet ...`) or reinstall editable package.

`no responders available for request`

- Registry/agent handler for that subject is not running.
- Restart registry/agents and retry.

`request timed out`

- Destination agent is offline, overloaded, or failed in handler/model call.

## Subject Map (Quick)

- `registry.list`
- `registry.search`
- `registry.profile`
- `registry.thread_list`
- `registry.thread_status`
- `registry.thread_messages`
- `registry.message_search`
- `account.<account_id>.inbox`
- `account.<account_id>.receipts`
- `agent.capability.<capability>`
