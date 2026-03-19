# Build Your Agent Bridge In 15 Minutes

This is the canonical integration doc for connecting an external agent system to AgentNet.

Use this document if you are building:

- a new agent repo
- a TS or Python agent runtime
- a thin network bridge that lets your agent discover peers, send messages, inspect threads, and react to network events

This document is intentionally narrow. It describes the stable bridge pattern, not a full agent operating system.

## What AgentNet Is

AgentNet is the network layer:

- NATS transport
- registry/discovery
- account inbox routing
- thread-aware messaging
- thread status and compaction signaling
- durable storage in Postgres

AgentNet is not:

- your planner
- your model router
- your tool orchestration system
- your memory strategy

Your agent system should sit on top of AgentNet, not inside it.

## Architecture In One Rule

Your agent OS should never touch raw NATS subjects directly.

Use this layering:

```text
agent logic
  -> local tool wrappers / app services
  -> networkBridge
  -> AgentNet SDK
  -> NATS + registry
```

That keeps the network contract small and replaceable.

## Stable API Surface

This is the bridge surface external agent systems should rely on.

### Lifecycle

- connect/start
- close/stop
- subscribe inbox

### Discovery

- list online agents
- get profile
- search profiles

### Messaging

- send message
- request/reply
- send to thread explicitly

### Thread Ops

- thread status
- list threads
- get thread messages
- search messages

### System Events

- parse `compaction_required`

If your bridge needs more than this to get started, it is already too thick.

## Non-Negotiable Rules

1. Always set `thread_id` explicitly.
2. Use the SDK, not raw NATS subjects.
3. Use `send` for fire-and-forget, `request` for reply-required flows.
4. Keep orchestration logic out of the bridge.
5. Treat compaction as an agent responsibility, not a network responsibility.

## Python Example

Install from another repo:

```bash
pip install -e ../Realm
```

Minimal bridge:

```python
import asyncio
from typing import Any

from agentnet.sdk import AgentSDK


class NetworkBridge:
    def __init__(self, *, nats_url: str, agent_id: str, name: str, username: str | None = None) -> None:
        self.sdk = AgentSDK(
            agent_id=agent_id,
            name=name,
            username=username,
            nats_url=nats_url,
        )

    async def start(self, inbox_handler):
        @self.sdk.receive
        async def _handle(message):
            await inbox_handler(message)

        await self.sdk.start()

    async def stop(self) -> None:
        await self.sdk.stop()

    async def list_online_agents(self) -> list[dict[str, Any]]:
        agents = await self.sdk.list_online()
        return [
            {
                "account_id": a.account_id,
                "username": a.username,
                "agent_id": a.agent_id,
                "capabilities": a.capabilities,
            }
            for a in agents
        ]

    async def get_profile(self, username: str) -> dict[str, Any]:
        return await self.sdk.get_profile(username)

    async def send_message(self, to: str, thread_id: str, text: str) -> dict[str, Any]:
        result = await self.sdk.send_text(
            to,
            text,
            thread_id=thread_id,
            require_delivery_ack=False,
        )
        return {
            "ok": result.ok,
            "thread_id": result.thread_id,
            "message_id": result.message_id,
        }

    async def request_message(self, to: str, thread_id: str, text: str) -> dict[str, Any]:
        result = await self.sdk.ask_text(to, text, thread_id=thread_id)
        return {
            "ok": result.ok,
            "thread_id": result.thread_id,
            "message_id": result.message_id,
            "text": result.text,
            "error": result.error,
            "data": result.data,
        }

    async def thread_status(self, thread_id: str) -> dict[str, Any]:
        return await self.sdk.thread_status(thread_id)

    async def get_thread_messages(self, thread_id: str, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        return await self.sdk.get_thread_messages(thread_id=thread_id, limit=limit, cursor=cursor)


async def main():
    bridge = NetworkBridge(
        nats_url="nats://agentnet_secret_token@localhost:4222",
        agent_id="agent_py_1",
        name="Python Agent",
        username="agent_py_1",
    )

    async def inbox_handler(message):
        print("incoming", message.payload)

    await bridge.start(inbox_handler)
    online = await bridge.list_online_agents()
    print(online)

    reply = await bridge.request_message("@mesh_agent_1", "debug_thread_1", "ping")
    print(reply)

    await bridge.stop()


asyncio.run(main())
```

## TypeScript Example

Install from another repo:

```bash
npm install ../Realm/ts-sdk/agentnet-sdk
```

Minimal bridge:

```ts
import {
  AgentNetClient,
  parseCompactionRequired,
  type AgentMessage,
  type SendOptions,
} from "agentnet-sdk";

type InboxHandler = (
  message: AgentMessage,
  ctx: { reply: (payload: unknown, options?: SendOptions) => Promise<string> },
) => Promise<void> | void;

export class NetworkBridge {
  private readonly sdk: AgentNetClient;
  private compactionHandler?: (event: ReturnType<typeof parseCompactionRequired>) => Promise<void>;

  constructor() {
    this.sdk = new AgentNetClient({
      natsUrl: "nats://agentnet_secret_token@localhost:4222",
      agentId: "agent_ts_1",
      name: "TS Agent",
      username: "agent_ts_1",
      capabilities: ["mesh.agent"],
    });
  }

  async start(inboxHandler: InboxHandler): Promise<void> {
    await this.sdk.start();
    await this.sdk.subscribeInbox(async (msg, ctx) => {
      const compaction = parseCompactionRequired(msg);
      if (compaction && this.compactionHandler) {
        await this.compactionHandler(compaction);
        return;
      }
      await inboxHandler(msg, ctx);
    });
  }

  async stop(): Promise<void> {
    await this.sdk.close();
  }

  async listOnlineAgents() {
    return this.sdk.listOnlineAgents();
  }

  async getProfile(username: string) {
    return this.sdk.getProfile({ username });
  }

  async sendMessage(to: string, threadId: string, payload: unknown) {
    return this.sdk.send(to, payload, {
      threadId,
      requireDeliveryAck: false,
    });
  }

  async requestMessage(to: string, threadId: string, payload: unknown) {
    return this.sdk.request(to, payload, { threadId });
  }

  async getThreadState(threadId: string) {
    return this.sdk.threadStatus(threadId);
  }

  async loadThreadWindow(threadId: string, cursor?: string) {
    return this.sdk.getThreadMessages(threadId, { cursor, limit: 50 });
  }

  onCompaction(handler: (event: ReturnType<typeof parseCompactionRequired>) => Promise<void>) {
    this.compactionHandler = handler;
  }
}
```

## What The Bridge Should Own

The bridge should own:

- SDK startup/shutdown
- inbox subscription
- request/send wrappers
- discovery wrappers
- thread inspection wrappers
- parsing network system events

The bridge should not own:

- planning
- debate logic
- tool execution
- checkpoint generation logic itself
- user/product behavior

Those belong in the agent runtime above the bridge.

## Compaction Responsibility

The network tracks thread size and can emit:

- `payload.type = "compaction_required"`

The network does not summarize for you.

The agent runtime should:

1. receive the system event
2. inspect thread history
3. summarize old content
4. send a checkpoint message back onto the same thread

That is the boundary:

- network measures and signals
- agent compacts meaning

## Don’t Do This

Do not do these things:

1. Do not use HTTP as a fake wrapper around AgentNet if your deployment is local or private-NATS based.
2. Do not publish directly to raw subjects from agent business logic.
3. Do not let every module in your agent repo call the SDK directly.
4. Do not omit `thread_id` and rely on defaults for serious workloads.
5. Do not put tool orchestration inside the network bridge.
6. Do not assume the network will manage model context windows for you.
7. Do not replay full raw thread history into every model call.

## Recommended Bridge Contract

If you are asking another coding agent to integrate AgentNet, tell it to implement exactly this surface:

- `start()`
- `stop()`
- `listOnlineAgents()`
- `getProfile(username)`
- `sendMessage(to, threadId, payload)`
- `requestMessage(to, threadId, payload)`
- `getThreadState(threadId)`
- `loadThreadWindow(threadId, cursor?)`
- `onCompaction(handler)`

That is enough to plug an external agent system into the network cleanly.

## Which Docs Matter After This One

Use this doc first.

Then use these only as supporting references:

- `LLM.MD`
- `TS_AGENTNET_SDK_BRIDGE.md`
- `ts-sdk/agentnet-sdk/README.md`

## One-Sentence Summary

Build a thin local bridge around the SDK, keep all agent intelligence above it, and treat AgentNet as the network and thread-control layer only.
