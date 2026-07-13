# apps/ — userland applications

Home for Realm **userland** services that sit above the kernel bus.

## Here / planned

| App | Role |
|-----|------|
| **[`gateway/`](gateway/)** | FastAPI HTTP API (API keys → server-side `AgentSDK.delegate_task`); see [docs/http-gateway.md](../docs/http-gateway.md) |
| Telegram gateway | Package entry `realm-telegram-gateway`; may relocate under `apps/` later |
| Admin / stand-up UI | Task board, agent status, thread browser |

## Not here

- Kernel protocol/SDK → `src/agentnet/`
- Process table (registry) → `services/registry/`
- MCP tool bridges → `drivers/mcp/`
- Boot/init (compose, shell) → `boot/`
- Experiments / one-off data work → `distro/`

Keep apps thin: identity, jobs, and discovery stay on the Realm bus; apps are clients of that bus, not a second control plane.
