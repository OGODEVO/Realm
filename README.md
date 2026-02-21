# AgentNet (NATS Agent Network)

`agentnet` is a lightweight Python package that lets agents discover each other and exchange direct messages over NATS.

## Features

- Agents join the network with a single `AgentNode`
- Direct inbox per agent (`agent.<id>.inbox`)
- Online registry with agent metadata and capabilities
- Async-first API for scripts and long-running workers

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
- Registry service subscribed to `registry.hello`, `registry.goodbye`, and `registry.list`

### 3) Integrate into your agent (5-10 lines)

```python
import asyncio
from agentnet import AgentNode

async def main():
    node = AgentNode(agent_id="agent_foo", name="FooAgent", capabilities=["chat"], nats_url="nats://localhost:4222")

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

agents = await list_online_agents("nats://localhost:4222")
print([a.to_dict() for a in agents])
```

CLI:

```bash
python -m agentnet list --nats-url nats://localhost:4222
# or, after install:
agentnet list
```

## Protocol (subjects + JSON schema)

### Subjects

- Direct messages: `agent.<id>.inbox`
- Presence hello: `registry.hello`
- Presence goodbye: `registry.goodbye`
- List request: `registry.list` (request/reply)

### Direct message JSON

```json
{
  "message_id": "b3d0...",
  "from_agent": "agent_a",
  "to_agent": "agent_b",
  "payload": {"text": "yo"},
  "sent_at": "2026-02-21T08:00:00Z",
  "kind": "direct"
}
```

### Presence hello JSON

```json
{
  "agent_id": "agent_a",
  "name": "Agent A",
  "capabilities": ["chat", "summarize"],
  "metadata": {"team": "ops"},
  "last_seen": "2026-02-21T08:00:00Z"
}
```

## Heartbeat requirement (must-have)

Presence is kept accurate via heartbeat:

- Agents re-publish `registry.hello` every 10-15 seconds (default: 12 seconds)
- Registry evicts agents with no heartbeat for 30-45 seconds (default TTL: 40 seconds)

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
