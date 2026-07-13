# mcp-server/ (moved)

MCP drivers now live under **`drivers/mcp/`**.

| File | Path |
|------|------|
| Realm MCP bridge | [`drivers/mcp/realm-mcp.py`](../drivers/mcp/realm-mcp.py) |
| Agent launcher | [`drivers/mcp/realm-agent-launcher.py`](../drivers/mcp/realm-agent-launcher.py) |
| Collaborator | [`drivers/mcp/realm-collaborator.py`](../drivers/mcp/realm-collaborator.py) |

Run (from repo root):

```bash
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222 \
  python drivers/mcp/realm-mcp.py
```

This directory is kept as a **pointer** so old docs and muscle memory still resolve.
