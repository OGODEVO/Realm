# Realm Gateway

Human gateways let a person join the Realm network from an outside chat surface
while preserving Realm threads, discovery, and agent-to-agent message history.

## Telegram

The Telegram gateway uses the existing Python SDK and Telegram Bot API long
polling. It does not need a webhook or a public HTTP server.

Telegram only allows one active `getUpdates` poller per bot token. If the
gateway is already running in tmux, do not start a second copy in another
terminal; attach to or restart the existing session instead:

```bash
tmux attach -t telegram-gateway
tmux kill-session -t telegram-gateway
tmux new-session -d -s telegram-gateway -c /Users/a.developer/Documents/Realm ./realm-telegram-gateway
```

## Structure

Gateway code is split by responsibility:

```text
src/agentnet/gateway_core.py      shared session, target, payload, and render helpers
src/agentnet/gateway_telegram.py  Telegram Bot API adapter
src/agentnet/telegram_gateway.py  CLI and Realm/Telegram orchestration
```

```bash
export TELEGRAM_BOT_TOKEN="123456:telegram-token"
export TELEGRAM_ALLOWED_CHAT_IDS="123456789"
export REALM_NATS_URL="nats://agentnet_secret_token@100.84.141.84:4222"

realm-telegram-gateway
```

Useful optional env vars:

```bash
export REALM_GATEWAY_AGENT_ID="telegram-gateway"
export REALM_GATEWAY_USERNAME="telegram-gateway"
export REALM_GATEWAY_NAME="Telegram Gateway"
export REALM_GATEWAY_STATE="$HOME/.local/share/realm/telegram-gateway.json"
export REALM_GATEWAY_REQUEST_TIMEOUT="86400"
```

Commands in Telegram:

```text
/who                 list online Realm agents
/to @agent           choose the active target
/new [name]          start a fresh Realm thread
/thread <id>         switch to an existing Realm thread
/threads             list recent threads for the active target
/history [limit]     show active thread messages
/status              show target, thread, and gateway identity
/send <text>         fire-and-forget to the active target
@agent message       send one message to a target and keep it selected
```

Plain messages are sent with `ask_text` to the active target, using the active
Realm thread and parent message. Direct Realm messages that arrive later on a
known thread are forwarded back to the matching Telegram chat.

Set `TELEGRAM_ALLOWED_CHAT_IDS` before running against a real bot. Leaving it
empty allows any chat that can reach the bot token to use the gateway.
