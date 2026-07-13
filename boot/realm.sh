#!/usr/bin/env bash
# realm.sh — friendlier shell over the same CLI as boot/network.sh
set -euo pipefail

BOOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NETWORK_SH="${BOOT_DIR}/network.sh"

usage() {
  cat <<'USAGE'
realm — Realm OS shell (thin wrapper over network / agentnet CLI)

Process table / presence:
  realm ps                 List online agents (alias: list, online)
  realm status @user       What is this agent doing? (alias: who)
  realm jobs [opts]        List jobs/tasks (alias: tasks)
  realm metrics            Registry health metrics

Jobs (same flags as network.sh tasks):
  realm jobs [--assignee ACCOUNT] [--coordinator ACCOUNT]
             [--parent TASK_ID] [--status STATUS] [--limit N]
  realm jobs --watch [--watch-interval 2] [--watch-seconds 0]
             Live job board (refresh until Ctrl-C)
  realm task <TASK_ID>     Task status + progress_history timeline
  realm task-status --task-id <TASK_ID>

Threads / messages:
  realm threads [--user USERNAME] [--query TEXT] [--limit N]
  realm thread <THREAD_ID>
  realm messages <THREAD_ID> [--limit N] [--cursor CURSOR]
  realm msearch [filters...]

Bus / ops:
  realm watch [inbox|receipts|<subject>]
  realm logs [registry|nats|postgres] [--tail N]
  realm help

Also accepted (network.sh compatibility):
  list, ls, online, tasks, agent-status, who, m, t, ts, msgs, w, l
USAGE
}

if [[ ! -x "$NETWORK_SH" && -f "$NETWORK_SH" ]]; then
  chmod +x "$NETWORK_SH" 2>/dev/null || true
fi

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift || true

case "$cmd" in
  help|-h|--help)
    usage
    ;;
  ps)
    exec "$NETWORK_SH" list "$@"
    ;;
  jobs)
    # Support: realm jobs --watch ...
    exec "$NETWORK_SH" tasks "$@"
    ;;
  task|task-status)
    # realm task <task_id>  OR  realm task --task-id <id>
    if [[ "$cmd" == "task" && $# -ge 1 && "${1:0:1}" != "-" ]]; then
      exec "$NETWORK_SH" task-status --task-id "$1" "${@:2}"
    fi
    exec "$NETWORK_SH" task-status "$@"
    ;;
  status|who|agent-status)
    exec "$NETWORK_SH" status "$@"
    ;;
  list|ls|online|tasks|metrics|m|threads|t|thread|ts|messages|msgs|msearch|message-search|watch|w|logs|l)
    exec "$NETWORK_SH" "$cmd" "$@"
    ;;
  *)
    # Pass through unknown cmds so new agentnet subcommands keep working
    if [[ "$cmd" == "help" ]]; then
      usage
      exit 0
    fi
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
