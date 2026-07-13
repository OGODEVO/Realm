# drivers/mcp — MCP drivers

Realm **drivers** that expose the job OS to coding agents via the Model Context Protocol.

| Server | File | Role |
|--------|------|------|
| Realm bridge | [`realm-mcp.py`](realm-mcp.py) | Discovery, chat, **jobs** (`delegate_task`, progress, await) |
| Agent launcher | [`realm-agent-launcher.py`](realm-agent-launcher.py) | Start/stop local OpenCode-backed workers |
| Collaborator | [`realm-collaborator.py`](realm-collaborator.py) | Chain / council multi-agent workflows |

```bash
# from repo root
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  python drivers/mcp/realm-mcp.py
```

Compat stubs remain under `mcp-server/` for old paths. Package import name stays **`agentnet`**.
