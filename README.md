# AgentNet (NATS Agent Network).

`agentnet` is a lightweight Python package that lets agents discover each other and exchange direct messages over NATS.

Network operator reference: [`NETWORK_CLI_GUIDE.md`](NETWORK_CLI_GUIDE.md)
UI integration reference: [`UI_BACKEND_MAPPING.md`](UI_BACKEND_MAPPING.md)

## Features

- Agents join the network with a single `AgentNode`
- Account inbox routing (`account.<account_id>.inbox`) with stable account identity
- Online registry with agent metadata and capabilities
- Thread-aware messaging (`thread_id`, `parent_message_id`)
- Discovery RPCs (`registry.search`, `registry.profile`)
- Thread discovery RPC (`registry.thread_list`)
- Thread history RPC (`registry.thread_messages`) with cursor pagination
- Message search RPC (`registry.message_search`) with filters
- Thread budget status RPC (`registry.thread_status`) for compaction signaling
- Envelope protocol versioning (`schema_version`) with backward-compatible defaulting
- Optional idempotency keys (`idempotency_key`) to suppress duplicate logical operations
- Delivery receipts with sender retry policy
- Async-first API for scripts and long-running workers
- Bounded in-process concurrency (worker cap + pending queue cap)
- Safety guardrails: TTL checks, dedupe, rate limiting, work timeouts, circuit breaker
- Durable metadata in Postgres (`agent_accounts`, `agent_sessions`, `agent_threads`, `agent_messages`)

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
- Registry service subscribed to `registry.register`, `registry.hello`, `registry.goodbye`, `registry.list`, `registry.search`, `registry.profile`, `registry.thread_list`, `registry.thread_messages`, `registry.message_search`, and `registry.thread_status`

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

## SDK v2 (agent-friendly)

Use the high-level SDK for common agent workflows:

```python
from agentnet.sdk import AgentSDK

async with AgentSDK(
    agent_id="agent_a",
    name="Agent A",
    nats_url="nats://agentnet_secret_token@localhost:4222",
) as sdk:
    online = await sdk.list_online()
    print([a.username for a in online])

    profile = await sdk.get_profile("mesh_agent_1")
    print(profile.get("bio", ""))

    result = await sdk.ask_text(
        "mesh_agent_1",
        "Analyze spread and total for Celtics @ Suns",
        thread_id="ops_1",
    )
    print(result.text)

    thread = sdk.thread("ops_1")
    await thread.send_text("mesh_agent_1", "Confirm your final pick")
```

LLM local tool wrappers can directly call:

- `sdk.list_online()`
- `sdk.get_profile(target)`
- `sdk.ask_text(to, text, thread_id=...)`
- `sdk.send_text(to, text, thread_id=...)`
- `sdk.list_threads(...)`
- `sdk.get_thread_messages(thread_id, limit=..., cursor=...)`
- `sdk.search_messages(...)`
- `sdk.thread_status(thread_id)`

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
agentnet search --nats-url nats://agentnet_secret_token@localhost:4222 --query weather --online-only
agentnet profile --nats-url nats://agentnet_secret_token@localhost:4222 --username weather_bot
```

Operator workflows (easy testing + visibility):

```bash
# Watch live inbox traffic (summary lines)
agentnet watch --nats-url nats://agentnet_secret_token@localhost:4222 --subject 'account.*.inbox'

# Watch delivery receipts
agentnet watch --nats-url nats://agentnet_secret_token@localhost:4222 --subject 'account.*.receipts'

# Join as an operator and chat interactively with an agent
agentnet chat --nats-url nats://agentnet_secret_token@localhost:4222 --to-username mesh_agent_1 --thread-id ops_thread_1

# Inspect a thread's token budget status (ok / warn / needs_compaction)
agentnet thread-status --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id ops_thread_1

# Discover old threads and pick one to resume
agentnet threads --nats-url nats://agentnet_secret_token@localhost:4222 --participant-username mesh_agent_1 --limit 20

# Page through messages inside one thread
agentnet thread-messages --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id ops_thread_1 --limit 50

# Search messages by filters
agentnet message-search --nats-url nats://agentnet_secret_token@localhost:4222 --thread-id ops_thread_1 --kind request --limit 50
```

`agentnet chat` commands:

- `/showthread` prints current thread id
- `/thread <id>` switches thread context
- `/quit` exits interactive mode

`agentnet threads` outputs:

- recent matching thread IDs
- per-thread status (`ok` / `warn` / `needs_compaction`)
- message/token counts and last activity time

`agentnet thread-status` outputs:

- `message_count`, `byte_count`, `approx_tokens`, `latest_checkpoint_end`
- status classification: `ok`, `warn`, `needs_compaction`
- thresholds from registry defaults or CLI overrides

## Security

AgentNet relies on **NATS Token Authentication** to ensure that unauthorized third parties cannot connect to your backend router and spoof AI agents. 

Your `docker-compose.yml` backend spins up requiring the default token `agentnet_secret_token`. You must prepend this token (like `nats://<TOKEN>@<IP>`) to every `AgentNode` or CLI command, or the server will instantly reject the TCP connection. Before deploying to production, ALWAYS change this `--auth` token in your docker network!

## Dev Auth (local key signing)

For local anti-spoof testing, you can enable dev auth:

1. Start registry with `DEV_AUTH=true` (compose env is supported):

```bash
DEV_AUTH=true docker compose -f docker/docker-compose.yml up -d --build registry
```
2. Set `DEV_AUTH=true` in agent env.
3. Each agent auto-creates a local key file in `.keys/<username-or-agent>.json`.
4. Register requests are signed (`dev-ed25519-v1`), and registry binds one public key per account.

If a different key later tries to register for the same account, registry rejects it with `auth_public_key_mismatch`.

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

Thread/message persistence:

- `agent_threads` stores thread metadata and participant account IDs.
- `agent_messages` stores message envelopes keyed by `message_id` with `thread_id` and `parent_message_id`.
- Registry persists messages by tapping NATS subjects (`account.*.inbox`, `agent.capability.*`, `_INBOX.>`).

## Protocol (subjects + JSON schema)

### Subjects

- Account messages: `account.<account_id>.inbox`
- Delivery receipts: `account.<account_id>.receipts`
- Register (request/reply): `registry.register`
- Account resolve (request/reply): `registry.resolve_account`
- Key resolve (request/reply): `registry.resolve_key`
- Search (request/reply): `registry.search`
- Profile (request/reply): `registry.profile`
- Thread list (request/reply): `registry.thread_list`
- Thread messages (request/reply): `registry.thread_messages`
- Message search (request/reply): `registry.message_search`
- Thread status (request/reply): `registry.thread_status`
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
  "thread_id": "thread_b3d0...",
  "parent_message_id": null,
  "kind": "direct",
  "schema_version": "1.1",
  "idempotency_key": "order_123_step_1",
  "auth": {
    "scheme": "dev-ed25519-v1",
    "public_key": "<base64url>",
    "claims": {"message_id": "b3d0...", "payload_sha256": "..."},
    "signature": "<base64url>"
  }
}
```

### Delivery receipt JSON

```json
{
  "message_id": "b3d0...",
  "status": "accepted",
  "event_at": "2026-02-21T08:00:00Z",
  "from_account_id": "acct_02...",
  "from_session_tag": "registry_01J...",
  "to_account_id": "acct_01...",
  "trace_id": "b3d0...",
  "thread_id": "thread_b3d0..."
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

`send_*()` waits for a delivery receipt by default and retries publish on receipt timeout (`default_send_retry_attempts=2`, `default_receipt_timeout_seconds=1.5`).

Incoming messages without `ttl_ms` or `expires_at` are rejected by default.

Thread budget env knobs (registry service):

- `THREAD_SOFT_LIMIT_TOKENS` (default `50000`)
- `THREAD_HARD_LIMIT_TOKENS` (default `60000`)
- `TOKEN_ESTIMATE_CHARS_PER_TOKEN` (default `4`)

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

## Agent Skeleton (YAML + prompts + tools)

This repo now includes a 3-agent skeleton that uses:

- per-agent system prompt files in `agents/prompts/`
- per-agent tool allowlists from `tools/`
- per-agent provider/model config from YAML
- OpenAI-compatible `/v1/chat/completions` and Claude `/v1/messages`

Files:

- `agents/agent_mesh_trio.py` (runner)
- `agents/config/mesh_agents.yaml` (agent/model/tool config)
- `agents/prompts/agent_1.txt`, `agents/prompts/agent_2.txt`, `agents/prompts/agent_3.txt`

Run:

```bash
cd /Users/klyexy/Documents/realm
source venv/bin/activate
set -a
source agents/.env
set +a
python agents/agent_mesh_trio.py --config agents/config/mesh_agents.yaml
```

Run a subset:

```bash
python agents/agent_mesh_trio.py --config agents/config/mesh_agents.yaml --only agent_1 --only agent_2
```

Required env vars for this skeleton:

- `OPENAI_API_KEY`, `OPENAI_BASE_URL`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`
- tool-side keys (if you call those tools): `RSC_TOKEN`, `ODDS_API_KEY`, `PERPLEXITY_API_KEY`
