#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/boot/docker-compose.yml"

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
  ./boot/network.sh list
  ./boot/network.sh status @username
  ./boot/network.sh tasks [--assignee ACCOUNT] [--coordinator ACCOUNT] [--parent TASK_ID] [--status STATUS] [--limit N]
                         [--watch] [--watch-interval SEC] [--watch-seconds SEC]
  ./boot/network.sh task-status --task-id TASK_ID [--raw]
  ./boot/network.sh metrics
  ./boot/network.sh threads [--user USERNAME] [--query TEXT] [--limit N]
  ./boot/network.sh thread <THREAD_ID>
  ./boot/network.sh messages <THREAD_ID> [--limit N] [--cursor CURSOR]
  ./boot/network.sh msearch [--thread THREAD_ID] [--kind KIND] [--from ACCOUNT_ID] [--to ACCOUNT_ID] [--from-ts ISO] [--to-ts ISO] [--limit N]
  ./boot/network.sh watch [inbox|receipts|<subject>]
  ./boot/network.sh logs [registry|nats|postgres] [--tail N]
  ./boot/network.sh help

Friendlier aliases: ./boot/realm.sh (ps, jobs, status)
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
  status|agent-status|who)
    target="${1:-}"
    [[ -z "$target" ]] && { echo "Usage: ./boot/network.sh status @username"; exit 2; }
    shift || true
    run_agentnet agent-status "$target" "$@"
    ;;
  tasks)
    assignee=""; coordinator=""; parent=""; status=""; limit="20"
    watch=""; watch_interval=""; watch_seconds=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --assignee) assignee="${2:-}"; shift 2 ;;
        --coordinator) coordinator="${2:-}"; shift 2 ;;
        --parent|--parent-task-id) parent="${2:-}"; shift 2 ;;
        --status) status="${2:-}"; shift 2 ;;
        --limit) limit="${2:-20}"; shift 2 ;;
        --watch) watch="1"; shift ;;
        --watch-interval) watch_interval="${2:-2}"; shift 2 ;;
        --watch-seconds) watch_seconds="${2:-0}"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 2 ;;
      esac
    done
    args=(tasks --limit "$limit")
    [[ -n "$assignee" ]] && args+=(--assignee-account-id "$assignee")
    [[ -n "$coordinator" ]] && args+=(--coordinator-account-id "$coordinator")
    [[ -n "$parent" ]] && args+=(--parent-task-id "$parent")
    [[ -n "$status" ]] && args+=(--status "$status")
    [[ -n "$watch" ]] && args+=(--watch)
    [[ -n "$watch_interval" ]] && args+=(--watch-interval "$watch_interval")
    [[ -n "$watch_seconds" ]] && args+=(--watch-seconds "$watch_seconds")
    run_agentnet "${args[@]}"
    ;;
  task-status)
    run_agentnet task-status "$@"
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
  thread|ts)
    thread_id="${1:-}"
    [[ -z "$thread_id" ]] && { echo "Usage: ./boot/network.sh thread <THREAD_ID>"; exit 2; }
    shift || true
    run_agentnet thread-status --thread-id "$thread_id" "$@"
    ;;
  messages|msgs)
    thread_id="${1:-}"
    [[ -z "$thread_id" ]] && { echo "Usage: ./boot/network.sh messages <THREAD_ID> [--limit N] [--cursor CURSOR]"; exit 2; }
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
