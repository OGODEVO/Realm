#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME:-/Users/a.developer}/.local/bin"
export HOME="${HOME:-/Users/a.developer}"
export PATH="${PATH:-$DEFAULT_PATH}"

REALM_AGENT_ID="${REALM_AGENT_ID:-}"
if [ -z "$REALM_AGENT_ID" ] && [ -n "${REALM_AGENT_HOME:-}" ]; then
    REALM_AGENT_ID="$(basename "$REALM_AGENT_HOME")"
fi
if [ -z "$REALM_AGENT_ID" ]; then
    echo "REALM_AGENT_ID is required" >&2
    exit 2
fi

REALM_AGENT_HOME="${REALM_AGENT_HOME:-$HOME/.local/share/$REALM_AGENT_ID}"
REALM_AGENT_ENV="${REALM_AGENT_ENV:-$REALM_AGENT_HOME/.env}"

if [ -f "$REALM_AGENT_ENV" ]; then
    set -a
    . "$REALM_AGENT_ENV"
    set +a
fi

REALM_AGENT_NAME="${REALM_AGENT_NAME:-$REALM_AGENT_ID}"
REALM_USERNAME="${REALM_USERNAME:-$REALM_AGENT_ID}"
REALM_NATS_URL="${REALM_NATS_URL:-nats://agentnet_secret_token@localhost:4222}"

OPENCODE_PORT="${OPENCODE_PORT:-4196}"
OPENCODE_HOST="${OPENCODE_HOST:-127.0.0.1}"
OPENCODE_URL="${OPENCODE_URL:-http://$OPENCODE_HOST:$OPENCODE_PORT}"
OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
OPENCODE_PYTHON="${OPENCODE_PYTHON:-python3}"
OPENCODE_AGENT="${OPENCODE_AGENT:-general}"
OPENCODE_DIR="${OPENCODE_DIR:-$REALM_AGENT_HOME}"
OPENCODE_TIMEOUT="${OPENCODE_TIMEOUT:-86400}"
OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-opencode}"

REALM_REPO="${REALM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REALM_WRAPPER="${REALM_WRAPPER:-$REALM_REPO/examples/opencode_realm_agent.py}"
REALM_BLOB_DIR="${REALM_BLOB_DIR:-$REALM_AGENT_HOME/.blobs/agent}"
REALM_OPENCODE_SESSION_MAP="${REALM_OPENCODE_SESSION_MAP:-$REALM_AGENT_HOME/.realm/$REALM_AGENT_ID-sessions.json}"

XDG_DATA_HOME="${XDG_DATA_HOME:-$REALM_AGENT_HOME/xdg-data}"
XDG_CACHE_HOME="${XDG_CACHE_HOME:-$REALM_AGENT_HOME/xdg-cache}"
OPENCODE_CONFIG="${OPENCODE_CONFIG:-$REALM_AGENT_HOME/opencode.json}"

OPENCODE_LOG="${OPENCODE_LOG:-/tmp/$REALM_AGENT_ID-opencode.log}"
REALM_LOG="${REALM_LOG:-/tmp/$REALM_AGENT_ID-realm.log}"
AUTH_SOURCE="${OPENCODE_AUTH_SOURCE:-$HOME/.local/share/opencode/auth.json}"
AUTH_TARGET="${XDG_DATA_HOME}/opencode/auth.json"

if [ -n "${REALM_SYSTEM_PROMPT_FILE:-}" ]; then
    REALM_SYSTEM_PROMPT="$(cat "$REALM_SYSTEM_PROMPT_FILE")"
fi
REALM_SYSTEM_PROMPT="${REALM_SYSTEM_PROMPT:-You are agent $REALM_AGENT_ID on the Realm network.}"

rm -f "$OPENCODE_LOG" "$REALM_LOG"
mkdir -p "$REALM_AGENT_HOME/.realm" "$REALM_AGENT_HOME/.blobs" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
mkdir -p "$(dirname "$AUTH_TARGET")"
cp "$AUTH_SOURCE" "$AUTH_TARGET" 2>/dev/null || true
cd "$REALM_AGENT_HOME"

cleanup() {
    if [ -n "${SERVER_PID:-}" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "${AGENT_PID:-}" ]; then
        kill "$AGENT_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT TERM INT HUP

env -i \
    HOME="$HOME" \
    PATH="$PATH" \
    GITHUB_PERSONAL_ACCESS_TOKEN="${GITHUB_PERSONAL_ACCESS_TOKEN:-}" \
    XDG_DATA_HOME="$XDG_DATA_HOME" \
    XDG_CACHE_HOME="$XDG_CACHE_HOME" \
    OPENCODE_CONFIG="$OPENCODE_CONFIG" \
    "$OPENCODE_BIN" serve --hostname "$OPENCODE_HOST" --port "$OPENCODE_PORT" --print-logs \
    >> "$OPENCODE_LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if lsof -iTCP:"$OPENCODE_PORT" -sTCP:LISTEN -P >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! lsof -iTCP:"$OPENCODE_PORT" -sTCP:LISTEN -P >/dev/null 2>&1; then
    echo "$REALM_AGENT_ID OpenCode server did not start on $OPENCODE_URL" >> "$REALM_LOG"
    exit 1
fi

env -i \
    HOME="$HOME" \
    PATH="$PATH" \
    XDG_DATA_HOME="$XDG_DATA_HOME" \
    XDG_CACHE_HOME="$XDG_CACHE_HOME" \
    REALM_NATS_URL="$REALM_NATS_URL" \
    REALM_AGENT_ID="$REALM_AGENT_ID" \
    REALM_AGENT_NAME="$REALM_AGENT_NAME" \
    REALM_USERNAME="$REALM_USERNAME" \
    REALM_BLOB_DIR="$REALM_BLOB_DIR" \
    REALM_OPENCODE_SESSION_MAP="$REALM_OPENCODE_SESSION_MAP" \
    REALM_SYSTEM_PROMPT="$REALM_SYSTEM_PROMPT" \
    REALM_MAX_AGENT_TURNS="${REALM_MAX_AGENT_TURNS:-56}" \
    OPENCODE_TIMEOUT="$OPENCODE_TIMEOUT" \
    OPENCODE_URL="$OPENCODE_URL" \
    OPENCODE_SERVER_USERNAME="$OPENCODE_SERVER_USERNAME" \
    OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-}" \
    OPENCODE_MODEL="${OPENCODE_MODEL:-}" \
    OPENCODE_AGENT="$OPENCODE_AGENT" \
    OPENCODE_DIR="$OPENCODE_DIR" \
    "$OPENCODE_PYTHON" "$REALM_WRAPPER" \
    >> "$REALM_LOG" 2>&1 &
AGENT_PID=$!

wait "$AGENT_PID"
