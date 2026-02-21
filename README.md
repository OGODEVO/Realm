# AgentNet (NATS Agent Network)

`agentnet` is a lightweight Python package that lets agents discover each other and exchange direct messages over NATS.

## Features

- Agents join the network with a single `AgentNode`
- Direct inbox per agent (`agent.<id>.inbox`)
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
await node.send(to="agent_bar", payload={"text": "yo"})
```

Under the hood this publishes to:

- `agent.agent_bar.inbox`

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

## Security

AgentNet relies on **NATS Token Authentication** to ensure that unauthorized third parties cannot connect to your backend router and spoof AI agents. 

Your `docker-compose.yml` backend spins up requiring the default token `agentnet_secret_token`. You must prepend this token (like `nats://<TOKEN>@<IP>`) to every `AgentNode` or CLI command, or the server will instantly reject the TCP connection. Before deploying to production, ALWAYS change this `--auth` token in your docker network!

## Durable sessions

Registry persists session metadata into Postgres table `agent_sessions`:

- `session_tag` (PK)
- `agent_id`
- `server_id`
- `connected_at`
- `disconnected_at`
- `last_seen`
- `status`
- `metadata` (JSONB)

Retention is controlled by `SESSION_RETENTION_DAYS` (default: `14`). Offline sessions older than retention are pruned.

## Protocol (subjects + JSON schema)

### Subjects

- Direct messages: `agent.<id>.inbox`
- Register (request/reply): `registry.register`
- Presence hello: `registry.hello`
- Presence goodbye: `registry.goodbye`
- List request: `registry.list` (request/reply)

### Direct message JSON

```json
{
  "message_id": "b3d0...",
  "from_agent": "agent_a",
  "from_session_tag": "registry_01J...",
  "to_agent": "agent_b",
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
  "session_tag": "registry_01J...",
  "heartbeat_interval": 12.0,
  "ttl_seconds": 40.0
}
```

`agent_id` is the logical role name. `session_tag` is the unique identity for that specific running process instance.

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
