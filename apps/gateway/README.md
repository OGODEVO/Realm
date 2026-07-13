# Realm HTTP Gateway (app)

Thin FastAPI app that turns HTTP into Realm **jobs**.  
It does **not** run agent brains. It uses the Python **AgentSDK** on the server.

## Who talks how

| Who | How they connect | Auth |
|-----|------------------|------|
| External clients / partners | REST (`/v1/...`) | API key header |
| Your engineers (curl, scripts) | Same REST | API key or service token |
| This gateway process | NATS via AgentSDK | `REALM_NATS_URL` mesh token |
| Browsers / mobile | REST only | **Never** expose NATS |

```text
Client  --API key-->  FastAPI gateway  --AgentSDK-->  Realm bus  -->  @agent inbox
```

## Run (dev)

```bash
# mesh up
docker compose -f boot/docker-compose.yml up -d
# or: docker compose -f boot/docker-compose.yml up -d

export REALM_NATS_URL=nats://agentnet_secret_token@127.0.0.1:4222
export GATEWAY_API_KEYS=dev-key-change-me
export PYTHONPATH=src

pip install fastapi uvicorn
uvicorn apps.gateway.main:app --reload --port 8080
```

## Smoke

```bash
curl -s localhost:8080/health

curl -s localhost:8080/v1/jobs \
  -H "content-type: application/json" \
  -H "x-api-key: dev-key-change-me" \
  -d '{
    "to": "@order_agent",
    "title": "order.create",
    "text": "Create demo order",
    "metadata": {"sku": "WB-BURGER", "qty": 2}
  }'
```

Convenience routes (same as jobs with fixed agents):

- `POST /v1/orders`
- `POST /v1/refunds`
- `POST /v1/inventory/low`
- `POST /v1/support/tickets`
- `POST /v1/shipping/delay`

Workers must be online and registered under those `@usernames` (or change routes).

## SDK vs login

- **SDK** = for *services you run* (this gateway, internal workers). Import `agentnet.sdk.AgentSDK`.
- **Login for APIs** = API keys / bearer tokens on HTTP. Issue keys to engineers and partners.
- **Login for mesh** = NATS URL token + agent register. Only infra and agent processes need this.
