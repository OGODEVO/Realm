# AgentNet (NATS Agent Network).

`agentnet` is a lightweight Python package that lets agents discover each other and exchange direct messages over NATS.

## Features

- Agents join the network with a single `AgentNode`
- Account inbox routing (`account.<account_id>.inbox`) with stable account identity
- Online registry with agent metadata and capabilities
- Async-first API for scripts and long-running workers
- Bounded in-process concurrency (worker cap + pending queue cap)
- Safety guardrails: TTL checks, dedupe, rate limiting, work timeouts, circuit breaker
- Durable session metadata in Postgres (`agent_sessions`)

## Quickstart

### 1) Install

```bash
pip install agentnet
```

### 2) Start NATS + Registry

From this repo:

```bash
docker compose -f docker/docker-compose.yml up -d
```

This starts:

- NATS on `localhost:4222`
- Postgres for durable session metadata
- Registry service subscribed to `registry.register`, `registry.hello`, `registry.goodbye`, and `registry.list`

### 3) Integrate into your agent (5-10 lines)

```python
import asyncio
from agentnet import AgentNode

async def main():
    # Notice the security token is passed directly in the URL!
    node = AgentNode(
        agent_id="agent_foo", 
        name="FooAgent", 
        capabilities=["chat"], 
        nats_url="nats://agentnet_secret_token@localhost:4222"
    )

    @node.on_message
    async def handle(msg):
        print("Got message:", msg.payload)

    await node.start_forever()

asyncio.run(main())
```

## Send a message

```python
await node.send(to="@weather_bot", payload={"text": "yo"})
await node.send_to_account(to_account_id="acct_01...", payload={"text": "yo"})
await node.send_to_username(username="weather_bot", payload={"text": "yo"})
```

Under the hood this publishes to:

- `account.acct_01....inbox`

## List online agents

Python API:

```python
from agentnet import list_online_agents

agents = await list_online_agents("nats://agentnet_secret_token@localhost:4222")
print([a.to_dict() for a in agents])
```

CLI:

```bash
python -m agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
# or, after install:
agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
```

Account-route message examples:

```bash
agentnet send --nats-url nats://agentnet_secret_token@localhost:4222 --to-username weather_bot '{"text":"yo"}'
agentnet request --nats-url nats://agentnet_secret_token@localhost:4222 --to-account acct_01abc... '{"text":"ping"}'
```

## Security

AgentNet relies on **NATS Token Authentication** to ensure that unauthorized third parties cannot connect to your backend router and spoof AI agents. 

Your `docker-compose.yml` backend spins up requiring the default token `agentnet_secret_token`. You must prepend this token (like `nats://<TOKEN>@<IP>`) to every `AgentNode` or CLI command, or the server will instantly reject the TCP connection. Before deploying to production, ALWAYS change this `--auth` token in your docker network!

## Durable identity + sessions

Registry persists account metadata into `agent_accounts` and session metadata into `agent_sessions`.

`agent_accounts`:

- `account_id` (PK)
- `username` (unique)
- `display_name`
- `bio`
- `capabilities` (JSONB)
- `metadata` (JSONB)
- `visibility`
- `status`
- `created_at`
- `updated_at`

`agent_sessions`:

- `session_tag` (PK)
- `agent_id`
- `account_id`
- `username`
- `server_id`
- `connected_at`
- `disconnected_at`
- `last_seen`
- `status`
- `metadata` (JSONB)

Retention is controlled by `SESSION_RETENTION_DAYS` (default: `14`). Offline sessions older than retention are pruned.

## Protocol (subjects + JSON schema)

### Subjects

- Account messages: `account.<account_id>.inbox`
- Register (request/reply): `registry.register`
- Account resolve (request/reply): `registry.resolve_account`
- Presence hello: `registry.hello`
- Presence goodbye: `registry.goodbye`
- List request: `registry.list` (request/reply)

### Direct message JSON

```json
{
  "message_id": "b3d0...",
  "from_agent": "agent_a",
  "from_account_id": "acct_01...",
  "from_session_tag": "registry_01J...",
  "to_agent": "agent_b",
  "to_account_id": "acct_02...",
  "payload": {"text": "yo"},
  "sent_at": "2026-02-21T08:00:00Z",
  "ttl_ms": 30000,
  "expires_at": "2026-02-21T08:00:30Z",
  "trace_id": "b3d0...",
  "kind": "direct"
}
```

### Presence hello JSON

```json
{
  "agent_id": "agent_a",
  "account_id": "acct_01...",
  "username": "agent_a",
  "name": "Agent A",
  "session_tag": "registry_01J...",
  "capabilities": ["chat", "summarize"],
  "metadata": {"team": "ops"},
  "last_seen": "2026-02-21T08:00:00Z"
}
```

### Register reply JSON

```json
{
  "account_id": "acct_01...",
  "username": "agent_a",
  "session_tag": "registry_01J...",
  "heartbeat_interval": 12.0,
  "ttl_seconds": 40.0
}
```

`agent_id` is the logical role label. `account_id` is stable routing identity. `session_tag` is the unique identity for that specific running process instance.

`send()` / `request()` are account-routed only. Use account targets (`to="account:acct_..."`, `to="acct_..."`) or usernames (`to="@name"` or `to="name"`). You can also call `send_to_account()`, `request_account()`, `send_to_username()`, and `request_username()` directly.

Incoming messages without `ttl_ms` or `expires_at` are rejected by default.

## Heartbeat requirement (must-have)

Presence is kept accurate via heartbeat:

- Agents re-publish `registry.hello` every 10-15 seconds (default: 12 seconds)
- Registry evicts agents with no heartbeat for 30-45 seconds (default TTL: 40 seconds)

## Concurrency guardrails

`AgentNode` enforces bounded in-process concurrency and backpressure by default:

- `max_concurrency=4`
- `max_pending=100`
- `work_timeout_seconds=30`
- `max_payload_bytes=256000`
- `rate_limit_per_sender_per_sec=5` with `rate_limit_burst=10`
- `dedupe_ttl_seconds=600`
- `circuit_breaker_failures=5` with `circuit_breaker_reset_seconds=15`

You can override these in `AgentNode(...)` per agent process.

## Wrapper SDK (integration-friendly)

Use `AgentWrapper` if you want a stable adapter API for embedding AgentNet in your own agent runtime:

```python
import asyncio
from agentnet import AgentWrapper


async def main() -> None:
    sdk = AgentWrapper(
        agent_id="demo_worker",
        name="Demo Worker",
        username="demo_worker",
        nats_url="nats://agentnet_secret_token@localhost:4222",
    )

    @sdk.receive
    async def on_message(msg):
        if msg.reply_to:
            await sdk.reply(request=msg, payload={"ok": True})

    await sdk.connect()
    await sdk.request(to_username="weather_bot", payload={"text": "status?"}, timeout=5.0)
    await sdk.close()


asyncio.run(main())
```

## Run examples

In this repo:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
python examples/agent_a.py
python examples/agent_b.py
python examples/list_agents.py
```

## Run LLM agent demo (Novita provider)

1. Install dependencies and start infra:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
docker compose -f docker/docker-compose.yml up -d --build
```

2. Create env file and add your key:

```bash
cp agents/.env.example agents/.env
```

Set `NOVITA_API_KEY` in `agents/.env`.

3. Start Agent B (responder) in terminal 1:

```bash
set -a
source agents/.env
set +a
python agents/agent_novita_b.py
```

4. Run Agent A (initiator) in terminal 2:

```bash
set -a
source agents/.env
set +a
python agents/agent_novita_a.py
```

Agent A will generate questions with `zai-org/glm-5`, send them to Agent B over AgentNet, and print turn-by-turn replies.

For cleaner recording output, tune these in `agents/.env`:

- `DEMO_TURNS=4` for a fuller back-and-forth
- `NOVITA_MAX_TOKENS=256` for shorter answers
- `LOG_TEXT_MAX_CHARS=700` to cap displayed text length per log block
- `TARGET_USERNAME=agent_novita_b` to control who Agent A talks to
