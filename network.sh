#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker/docker-compose.yml"

if [[ -f "${ROOT_DIR}/agents/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/agents/.env"
  set +a
fi

NATS_URL="${NATS_URL:-nats://agentnet_secret_token@localhost:4222}"

if [[ -x "${ROOT_DIR}/venv/bin/python" ]]; then
  AGENTNET_CMD=(env "PYTHONPATH=${ROOT_DIR}/src" "${ROOT_DIR}/venv/bin/python" -m agentnet)
else
  AGENTNET_CMD=(agentnet)
fi

usage() {
  cat <<'USAGE'
network.sh - simple bash wrapper for AgentNet

Usage:
  ./network.sh list
  ./network.sh metrics
  ./network.sh threads [--user USERNAME] [--query TEXT] [--limit N]
  ./network.sh thread <THREAD_ID>
  ./network.sh messages <THREAD_ID> [--limit N] [--cursor CURSOR]
  ./network.sh msearch [--thread THREAD_ID] [--kind KIND] [--from ACCOUNT_ID] [--to ACCOUNT_ID] [--from-ts ISO] [--to-ts ISO] [--limit N]
  ./network.sh watch [inbox|receipts|<subject>]
  ./network.sh logs [registry|nats|postgres] [--tail N]
  ./network.sh help
USAGE
}

run_agentnet() {
  "${AGENTNET_CMD[@]}" "$@" --nats-url "${NATS_URL}"
}

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift || true

case "$cmd" in
  help|-h|--help)
    usage
    ;;
  list|ls|online)
    run_agentnet list "$@"
    ;;
  metrics|m)
    run_agentnet metrics "$@"
    ;;
  threads|t)
    user=""; query=""; limit="20"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --user) user="${2:-}"; shift 2 ;;
        --query) query="${2:-}"; shift 2 ;;
        --limit) limit="${2:-20}"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 2 ;;
      esac
    done
    args=(threads --limit "$limit")
    [[ -n "$user" ]] && args+=(--participant-username "${user#@}")
    [[ -n "$query" ]] && args+=(--query "$query")
    run_agentnet "${args[@]}"
    ;;
  thread|status|ts)
    thread_id="${1:-}"
    [[ -z "$thread_id" ]] && { echo "Usage: ./network.sh thread <THREAD_ID>"; exit 2; }
    shift || true
    run_agentnet thread-status --thread-id "$thread_id" "$@"
    ;;
  messages|msgs)
    thread_id="${1:-}"
    [[ -z "$thread_id" ]] && { echo "Usage: ./network.sh messages <THREAD_ID> [--limit N] [--cursor CURSOR]"; exit 2; }
    shift || true
    run_agentnet thread-messages --thread-id "$thread_id" "$@"
    ;;
  msearch|message-search)
    thread_id=""; from_id=""; to_id=""; kind=""; from_ts=""; to_ts=""; limit="50"; cursor=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --thread) thread_id="${2:-}"; shift 2 ;;
        --from) from_id="${2:-}"; shift 2 ;;
        --to) to_id="${2:-}"; shift 2 ;;
        --kind) kind="${2:-}"; shift 2 ;;
        --from-ts) from_ts="${2:-}"; shift 2 ;;
        --to-ts) to_ts="${2:-}"; shift 2 ;;
        --limit) limit="${2:-50}"; shift 2 ;;
        --cursor) cursor="${2:-}"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 2 ;;
      esac
    done
    args=(message-search --limit "$limit")
    [[ -n "$thread_id" ]] && args+=(--thread-id "$thread_id")
    [[ -n "$from_id" ]] && args+=(--from-account-id "$from_id")
    [[ -n "$to_id" ]] && args+=(--to-account-id "$to_id")
    [[ -n "$kind" ]] && args+=(--kind "$kind")
    [[ -n "$from_ts" ]] && args+=(--from-ts "$from_ts")
    [[ -n "$to_ts" ]] && args+=(--to-ts "$to_ts")
    [[ -n "$cursor" ]] && args+=(--cursor "$cursor")
    run_agentnet "${args[@]}"
    ;;
  watch|w)
    subject="${1:-inbox}"
    shift || true
    [[ "$subject" == "inbox" ]] && subject="account.*.inbox"
    [[ "$subject" == "receipts" ]] && subject="account.*.receipts"
    run_agentnet watch --subject "$subject" "$@"
    ;;
  logs|l)
    service="${1:-registry}"
    shift || true
    tail_n="120"
    if [[ "${1:-}" == "--tail" ]]; then
      tail_n="${2:-120}"
      shift 2 || true
    fi
    docker compose -f "$COMPOSE_FILE" logs -f --tail "$tail_n" "$service"
    ;;
  *)
    echo "Unknown command: $cmd"
    usage
    exit 2
    ;;
esac
