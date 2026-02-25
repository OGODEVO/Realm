# LLM Integration Guide (AgentNet)

This doc is for developers using an LLM/code agent to integrate with this repo.
Use it as a source-of-truth prompt/context file.

## What This Repo Provides

- Local agent network over NATS (no HTTPS required for local usage)
- Python SDK for agent-to-agent communication
- Registry/discovery (`list_online`, `profile`, `search`)
- Thread-aware messaging (`thread_id`, `parent_message_id`)
- Thread budget status (`ok`, `warn`, `needs_compaction`)

Main package import path:

- `agentnet`
- high-level SDK: `agentnet.sdk.AgentSDK`

## Integration Modes

### Mode A: Use SDK from Another Repo (recommended)

Install this repo as a dependency in your own project.

```bash
# from your other repo
pip install -e /absolute/path/to/realm
```

Then import:

```python
from agentnet.sdk import AgentSDK
```

### Mode B: Clone and Run Full Local Stack

```bash
git clone <this-repo-url> realm
cd realm
python -m venv venv
source venv/bin/activate
pip install -e .
docker compose -f docker/docker-compose.yml up -d
```

NATS URL used by SDK/CLI:

```text
nats://agentnet_secret_token@localhost:4222
```

## Minimal SDK Usage

```python
import asyncio
from agentnet.sdk import AgentSDK

NATS_URL = "nats://agentnet_secret_token@localhost:4222"

async def main():
    async with AgentSDK(
        agent_id="my_agent",
        name="My Agent",
        nats_url=NATS_URL,
    ) as sdk:
        online = await sdk.list_online()
        print([a.username for a in online])

        profile = await sdk.get_profile("mesh_agent_1")
        print(profile.get("bio", ""))

        result = await sdk.ask_text(
            "mesh_agent_1",
            "Analyze spread and total for Celtics @ Suns and return one pick.",
            thread_id="ops_1",
        )
        print(result.text)

        status = await sdk.thread_status("ops_1")
        print(status["status"])  # ok | warn | needs_compaction

asyncio.run(main())
```

## LLM Tool Wrapper Template (Local Functions)

Use these in your LLM app; expose them as callable tools/functions.

```python
from typing import Any
from agentnet.sdk import AgentSDK

NATS_URL = "nats://agentnet_secret_token@localhost:4222"
sdk = AgentSDK(agent_id="llm_runtime", name="LLM Runtime", nats_url=NATS_URL)

async def tool_list_online() -> dict[str, Any]:
    agents = await sdk.list_online()
    return {
        "count": len(agents),
        "agents": [
            {
                "username": a.username,
                "agent_id": a.agent_id,
                "capabilities": a.capabilities,
                "account_id": a.account_id,
            }
            for a in agents
        ],
    }

async def tool_get_profile(username: str) -> dict[str, Any]:
    return await sdk.get_profile(username)

async def tool_ask_agent(username: str, text: str, thread_id: str) -> dict[str, Any]:
    res = await sdk.ask_text(username, text, thread_id=thread_id)
    return {
        "ok": res.ok,
        "text": res.text,
        "thread_id": res.thread_id,
        "message_id": res.message_id,
        "parent_message_id": res.parent_message_id,
        "data": res.data,
        "error": res.error,
    }

async def tool_send_agent(username: str, text: str, thread_id: str) -> dict[str, Any]:
    res = await sdk.send_text(username, text, thread_id=thread_id)
    return {
        "ok": res.ok,
        "thread_id": res.thread_id,
        "message_id": res.message_id,
        "parent_message_id": res.parent_message_id,
    }

async def tool_thread_status(thread_id: str) -> dict[str, Any]:
    return await sdk.thread_status(thread_id)
```

## Thread Best Practices

- Reuse the same `thread_id` for a single task/debate.
- Use `sdk.thread("thread_id")` when you want parent-message chaining.
- Check `thread_status` when running long tasks:
  - `ok`: continue
  - `warn`: prepare compaction checkpoint
  - `needs_compaction`: compact before next heavy context build

## Local-Only Architecture

- LLM process -> local wrapper function -> `AgentSDK` -> local NATS/registry
- No HTTP/HTTPS needed for local network communication
- External HTTP only occurs if your own tools call external APIs

## Quick Troubleshooting

### Cannot connect to NATS

- Ensure Docker services are running:

```bash
docker compose -f docker/docker-compose.yml up -d
```

- Verify URL/token:

```text
nats://agentnet_secret_token@localhost:4222
```

### `agentnet` command not found

- Use module form:

```bash
python -m agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
```

### Username/profile not found

- Discover first:

```bash
agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
```

Then request exact username.

## Recommended LLM Prompt Hint

If you want the LLM to use network tools correctly, include:

- "Use local AgentSDK tools, not HTTP."
- "Treat `thread_status` as the compaction signal."
- "Use `ask_text` for request/reply, `send_text` for fire-and-forget."
- "Always pass `thread_id`."

