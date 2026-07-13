# HTTP gateway — how FastAPI / apps integrate

External systems and engineers should reach Realm as a **job OS**, not as raw
NATS. This doc freezes the integration shape for HTTP apps (FastAPI and peers).

| Related | Purpose |
|---------|---------|
| [process-contract.md](process-contract.md) | Job lifecycle the gateway must not redefine |
| [architecture.md](architecture.md) | Apps vs kernel vs drivers |
| [../apps/gateway/README.md](../apps/gateway/README.md) | Dev gateway sketch (REST → jobs) |
| [../UI_BACKEND_MAPPING.md](../UI_BACKEND_MAPPING.md) | Subject map if you build a richer UI backend |

---

## 1. Who talks how

| Who | How they connect | Auth |
|-----|------------------|------|
| **External clients / partners** | REST (`/v1/...`) | HTTP API key (or bearer service token) |
| **Browsers / mobile** | REST (or WebSocket over your app) only | **Never** NATS from the browser |
| **Engineers (curl, scripts)** | Same REST with service tokens **or** Python `AgentSDK` for internal services | HTTP key **or** mesh token (if using SDK) |
| **Gateway process itself** | NATS via `AgentSDK` / `AgentWrapper` server-side | `REALM_NATS_URL` mesh token |
| **Mesh agents / workers** | NATS directly (SDK / MCP) | Mesh register + NATS token |

```text
┌──────────────┐   API key    ┌─────────────────┐  AgentSDK   ┌────────────┐
│ Client / UI  │ ───────────► │ FastAPI gateway │ ──────────► │ NATS mesh  │
│ curl / partner│             │ (publisher only)│             │ + registry │
└──────────────┘              └─────────────────┘             └─────┬──────┘
                                                                    │
                                                              task.assign
                                                                    ▼
                                                              @agent inbox
                                                                    │
                                                         progress → result
```

---

## 2. Gateway role (publisher only)

The HTTP service:

1. **Authenticates** the caller (API key / service token).
2. **Validates** the request body (target agent, title, text, metadata).
3. **Publishes work** with server-side `AgentSDK.delegate_task` (or equivalent
   `build_task_assign` + send to account inbox).
4. **Returns** `task_id` (and optional thread/message ids) so the client can poll.

The gateway does **not**:

- Run the LLM / coding brain for the job
- Own business side effects that belong in worker tools
- Replace `task_status` / registry as source of truth for completion
- Expose NATS credentials to the client

Completion remains the process contract: worker emits `task.progress*` then one
of `task.result` | `task.blocked` | `task.failed`. Clients poll
`GET /v1/jobs/{task_id}` (app) or use mesh `task_status` from internal SDK.

---

## 3. Server-side SDK usage

Inside the FastAPI process (trusted network):

```python
from agentnet.sdk import AgentSDK

# On startup: one long-lived SDK identity for the gateway service
sdk = AgentSDK(
    agent_id="http-gateway",
    name="HTTP Gateway",
    username="http-gateway",
    nats_url=os.environ["REALM_NATS_URL"],
    capabilities=["http-gateway"],
    metadata={"kind": "http-gateway", "role": "orchestrator"},
)
await sdk.__aenter__()  # or connect pattern you use in-app

# On POST /v1/jobs
result = await sdk.delegate_task(
    body["to"],           # "@order_agent"
    body["text"],
    title=body.get("title"),
    parent_task_id=body.get("parent_task_id"),
    metadata=body.get("metadata") or {},
    require_delivery_ack=False,  # poll registry for truth
)
# return {"task_id": result.data["task_id"], ...}
```

`AgentWrapper` is the lower stable adapter over `AgentNode` if you need tighter
control; `AgentSDK` is the ergonomic coordinator API (`delegate_task`,
`report_progress`, `list_tasks`, discovery).

Package import remains **`agentnet`** — do not invent a second client for apps.

---

## 4. Engineers: two legitimate paths

### A. REST + service token (recommended for most humans/scripts)

Same routes as external clients; issue engineers a key with appropriate scope.

```bash
curl -s localhost:8080/v1/jobs \
  -H "content-type: application/json" \
  -H "x-api-key: $GATEWAY_API_KEY" \
  -d '{"to":"@coder","title":"fix-n","text":"Investigate failing tests"}'
```

### B. Python SDK for internal services

Services that already run inside the mesh perimeter may use `AgentSDK` with
`REALM_NATS_URL` directly (workers, batch jobs, other apps). They still honor
the same `task.*` contract.

Do **not** give NATS mesh tokens to browsers, partner frontends, or untrusted
laptops when an HTTP gateway exists.

---

## 5. Auth layers (do not conflate)

| Layer | What it protects | Typical secret |
|-------|------------------|----------------|
| **HTTP API key / service token** | Who may call the gateway REST API | `x-api-key`, `Authorization: Bearer …` |
| **NATS mesh token** | Who may connect to the kernel bus | URL userinfo in `REALM_NATS_URL` (e.g. `nats://agentnet_secret_token@host:4222`) |
| **Agent register / identity** | Who this process is on the mesh | `username` / `account_id` / session via registry hello |

Rules:

1. Clients prove **HTTP** identity; the gateway proves **mesh** identity.
2. Rotating an API key must not require re-registering every worker.
3. Rotating the NATS token affects infra + agents + gateway process only.
4. Never embed the mesh token in frontend bundles or mobile apps.

---

## 6. Simple request flow

```text
1. Client  POST /v1/jobs  { to, title, text, metadata? }
2. Gateway checks x-api-key
3. Gateway  delegate_task(@to, text, title=…)
4. Mesh     delivers task.assign → account.{assignee}.inbox
5. Worker   progress (ack, working, tool…) → terminal
6. Registry indexes task events
7. Client   GET /v1/jobs/{task_id}  (gateway → registry task_status)
            until status ∈ {completed, blocked, failed}
```

Optional convenience routes (same as jobs with fixed `@agent` targets) may
exist for demos (`/v1/orders`, `/v1/refunds`, …) — they still call
`delegate_task`, not a separate completion protocol.

---

## 7. What to put in HTTP vs what stays on the mesh

| Concern | HTTP gateway | Mesh / workers |
|---------|--------------|----------------|
| Auth for outsiders | Yes | No |
| Input validation / rate limits | Yes | Optional |
| `delegate_task` | Server-side only | Coordinators / agents |
| Brain / tools / side effects | No | Yes (drivers + allowlist) |
| `task.progress` emission | No | Worker |
| Terminal result | Observed via status API | Emitted by worker |
| Thread history for UI | Optional read API via registry | Source messages on bus |

---

## 8. Dev pointers

Sketch app docs: [apps/gateway/README.md](../apps/gateway/README.md).

```bash
# mesh
docker compose -f boot/docker-compose.yml up -d

export REALM_NATS_URL=nats://agentnet_secret_token@127.0.0.1:4222
export GATEWAY_API_KEYS=dev-key-change-me
export PYTHONPATH=src

# when apps.gateway.main exists:
# uvicorn apps.gateway.main:app --reload --port 8080
```

Human Telegram path (not REST, still an **app**): see
[services/gateway/README.md](../services/gateway/README.md) and
`realm-telegram-gateway` — joins as a mesh process with role `human-gateway`.

---

## 9. Freeze summary

1. **External world → HTTP (+ keys).** Never NATS from browsers.
2. **Gateway → AgentSDK → `delegate_task`.** Publisher only.
3. **Jobs still finish on the process contract** (progress + one terminal).
4. **Two auth planes:** HTTP API key vs NATS mesh token.
5. **Engineers:** REST service tokens **or** internal Python SDK — same OS.
