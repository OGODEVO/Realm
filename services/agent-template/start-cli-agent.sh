#!/usr/bin/env bash
# Start a Codex- or Grok-backed Realm worker (no OpenCode server required).
# For OpenCode, use start-opencode-agent.sh instead.

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
    # shellcheck disable=SC1090
    . "$REALM_AGENT_ENV"
    set +a
fi

REALM_AGENT_NAME="${REALM_AGENT_NAME:-$REALM_AGENT_ID}"
REALM_USERNAME="${REALM_USERNAME:-$REALM_AGENT_ID}"
REALM_NATS_URL="${REALM_NATS_URL:-nats://agentnet_secret_token@localhost:4222}"
REALM_RUNTIME="${REALM_RUNTIME:-codex}"
REALM_WORKDIR="${REALM_WORKDIR:-${OPENCODE_DIR:-$REALM_AGENT_HOME}}"
REALM_PYTHON="${REALM_PYTHON:-${OPENCODE_PYTHON:-python3}}"

CODEX_BIN="${CODEX_BIN:-codex}"
GROK_BIN="${GROK_BIN:-grok}"

REALM_REPO="${REALM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REALM_WRAPPER="${REALM_WRAPPER:-$REALM_REPO/examples/cli_realm_agent.py}"
REALM_BLOB_DIR="${REALM_BLOB_DIR:-$REALM_AGENT_HOME/.blobs/agent}"
REALM_LOG="${REALM_LOG:-/tmp/$REALM_AGENT_ID-realm.log}"

if [ -n "${REALM_SYSTEM_PROMPT_FILE:-}" ] && [ -f "$REALM_SYSTEM_PROMPT_FILE" ]; then
    REALM_SYSTEM_PROMPT="$(cat "$REALM_SYSTEM_PROMPT_FILE")"
fi
REALM_SYSTEM_PROMPT="${REALM_SYSTEM_PROMPT:-You are agent $REALM_AGENT_ID on the Realm network (runtime=$REALM_RUNTIME).}"

if [ "$REALM_RUNTIME" = "opencode" ]; then
    echo "REALM_RUNTIME=opencode is not handled by start-cli-agent.sh." >&2
    echo "Use services/agent-template/start-opencode-agent.sh instead." >&2
    exit 2
fi

rm -f "$REALM_LOG"
mkdir -p "$REALM_AGENT_HOME/.realm" "$REALM_AGENT_HOME/.blobs" "$REALM_WORKDIR"
cd "$REALM_AGENT_HOME"

cleanup() {
    if [ -n "${AGENT_PID:-}" ]; then
        kill "$AGENT_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT HUP

export PYTHONPATH="${REALM_REPO}/src${PYTHONPATH:+:$PYTHONPATH}"

env -i \
    HOME="$HOME" \
    PATH="$PATH" \
    PYTHONPATH="$PYTHONPATH" \
    REALM_NATS_URL="$REALM_NATS_URL" \
    REALM_AGENT_ID="$REALM_AGENT_ID" \
    REALM_AGENT_NAME="$REALM_AGENT_NAME" \
    REALM_USERNAME="$REALM_USERNAME" \
    REALM_RUNTIME="$REALM_RUNTIME" \
    REALM_WORKDIR="$REALM_WORKDIR" \
    REALM_BLOB_DIR="$REALM_BLOB_DIR" \
    REALM_SYSTEM_PROMPT="$REALM_SYSTEM_PROMPT" \
    REALM_WORK_TIMEOUT_SECONDS="${REALM_WORK_TIMEOUT_SECONDS:-86400}" \
    REALM_BRAIN_TIMEOUT="${REALM_BRAIN_TIMEOUT:-86400}" \
    CODEX_BIN="$CODEX_BIN" \
    CODEX_MODEL="${CODEX_MODEL:-}" \
    CODEX_SANDBOX="${CODEX_SANDBOX:-workspace-write}" \
    CODEX_FULL_AUTO="${CODEX_FULL_AUTO:-}" \
    CODEX_JSON="${CODEX_JSON:-}" \
    CODEX_SKIP_GIT_CHECK="${CODEX_SKIP_GIT_CHECK:-true}" \
    GROK_BIN="$GROK_BIN" \
    GROK_MODEL="${GROK_MODEL:-}" \
    GROK_ALWAYS_APPROVE="${GROK_ALWAYS_APPROVE:-true}" \
    GROK_OUTPUT_FORMAT="${GROK_OUTPUT_FORMAT:-plain}" \
    GROK_MODE="${GROK_MODE:-agent}" \
    GITHUB_PERSONAL_ACCESS_TOKEN="${GITHUB_PERSONAL_ACCESS_TOKEN:-}" \
    OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
    XAI_API_KEY="${XAI_API_KEY:-}" \
    CODEX_HOME="${CODEX_HOME:-}" \
    "$REALM_PYTHON" "$REALM_WRAPPER" \
    >> "$REALM_LOG" 2>&1 &
AGENT_PID=$!

echo "$REALM_AGENT_ID starting runtime=$REALM_RUNTIME pid=$AGENT_PID log=$REALM_LOG" >&2
wait "$AGENT_PID"
