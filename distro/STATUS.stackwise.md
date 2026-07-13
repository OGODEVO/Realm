# Stackwise — Agent Mission Tracker

**Last update:** 2026-07-01T00:35:00Z

**Mission:** Turn stack-wise (iOS supplement tracking app) into a full business.
**Repo:** https://github.com/OGODEVO/stack-wise
**Local:** /Users/klyexy/Stackwise
**Canonical repo tracker:** /Users/klyexy/Stackwise/STATUS.md
**Canonical task ledger:** /Users/klyexy/Stackwise/AGENT_TASKS.md
**M2 worker state:** /Users/klyexy/.local/share/m2-agent/state/eng-m2.json
**Current branch:** `backend-post-merge-hardening` — backend post-merge audit/fixes

---

## CYCLE 4 — Post-Merge Review, Fixes & Quality

**Status:** PR #7, PR #8, PR #9, and PR #10 are merged. PR #10 was merged before m4-dl completed its audit. Backend is not deployed, so this is not production-impacting. Current task is post-merge backend audit/hardening before deployment.

### Backend Architecture Decision
- Use FastAPI + Postgres, not Supabase-first.
- iOS remains local-first with SwiftData.
- FastAPI owns sync APIs, auth verification, server-side validation, future AI bottle parsing, and webhook integrations.
- Postgres is the canonical synced data store.
- `Stackwise/Data/Supabase/schema.sql` is legacy reference only.
- Current scaffold includes health/readiness endpoints, supplement CRUD, check-in upsert/list, Docker Postgres, and an initial SQL migration.
- Current auth is a development shim using `X-Stackwise-User-Id`; production must replace this with Apple Sign-In token verification before deployment.

### Active Team
- **eng-m2** (M2 OpenCode agent) — primary coding agent; implements fixes/features, runs checks, opens PRs, updates status
- **@m4-dl** (M4 Mac Mini) — reviewer/auditor; examines eng-m2's code for vulnerabilities, bugs, compile risks, regressions, missing tests, privacy/security issues, and product-quality gaps
- **a.developer** — merged PRs #7 + #8; provisioning accounts continues

### Operating Model
- Use a coder/reviewer iteration loop, not feature splitting by default.
- Every wake cycle, agents read `STATUS.md` and `AGENT_TASKS.md`, then check the Stackwise Realm thread before acting.
- Agents reconstruct context: previous tasks, current task, current assignment, open blockers, and next expected action.
- Agents ask the teammate what is current if state is unclear, then split/assign the task at hand.
- eng-m2 builds or fixes the next highest-value item on a branch.
- eng-m2 sends @m4-dl a review packet with branch/PR, changed files, risk areas, and what to audit.
- @m4-dl reports findings back in the Realm thread.
- eng-m2 triages findings, fixes valid issues, verifies, and asks for another pass if needed.
- Iterate until there are no blocking findings, then move to the next task.
- Use `realm_send_text` + `realm_get_thread_messages` for this flow; do not use blocking `realm_ask_text` for long reviews.
- Agents update `AGENT_TASKS.md` before ending a work cycle.

### Post-Merge Findings
- **PR #7 (merge-all-prs):** Code is on main. No critical issues found.
- **PR #8 (ios-watch-integration):** Code is on main. One compilation issue found:
  - `PersistenceService.resetStore()` used `[any PersistentModel.Type]` existential array with `delete<T>(model: T.Type)` — type inference fails with existentials. Fixed by replacing loop with concrete-type helper.
  - All other code reviewed clean: WatchSessionManager, AppState IAP wiring, data reset, checkInteraction access.
- **PR #9 (cycle-4-post-merge):** Merged at 11:21:49 UTC. Contains the concrete SwiftData model deletion fix for `resetStore()` and status updates.

### What's Still Buildable (No Accounts)
- XCUITest foundation for UI testing — not started yet
- Localization string catalogs (.xcstrings)
- Accessibility audit (VoiceOver, Dynamic Type, Reduce Motion)
- Performance/load profiling

### Blocked On Accounts
- Apple Developer account: Sign In with Apple entitlement, App Store Connect products, StoreKit validation
- Backend hosting/Postgres: deploy FastAPI service + managed Postgres or VPS Postgres
- TelemetryDeck: app ID/API key
- Sentry: DSN
- RevenueCat: canonical purchase layer setup

### Next Agent Instruction
On the next heartbeat, eng-m2 should:
1. Read `/Users/klyexy/Stackwise/AGENT_TASKS.md` and continue task `stackwise-backend-hardening-001`.
2. Review the merged `backend/` FastAPI + Postgres scaffold on main.
3. Run feasible checks locally.
4. Send @m4-dl a review packet for the merged backend and ask them to audit for vulnerabilities, auth gaps, data-model issues, sync risks, compile/runtime risks, and missing tests.
5. Fix valid findings and iterate until clean.
6. Update both STATUS.md files and `AGENT_TASKS.md` with review requests, findings, fixes, and decisions.

### Live Agent State
- eng-m2 ACKed task `stackwise-backend-hardening-001` at 2026-06-13T18:59:15Z.
- m4-dl is online and emitted malformed ACK/WORKING messages for the ACK-only check itself, but actual backend audit pickup is not confirmed; do not block eng-m2.
- eng-m2 state updates are now written by heartbeat and the worker via `/Users/klyexy/.local/bin/agent-state-update`.

---

## Realm MCP Orchestration Update — 2026-07-01

**Status:** Network is reachable again over `nats://agentnet_secret_token@100.84.141.84:4222`. Registry store and DB queue are enabled. The current MCP surface includes the base Realm bridge, the agent launcher, and the collaborator/council tools.

### Added MCP Tools
- `realm_agent_launcher`: launches, lists, restarts, stops, and reports RAM/process stats for local OpenCode-backed Realm agents.
- `realm_collaborator`: coordinates `collaborate_chain` sequential handoffs and `collaborate_council` parallel council workflows.

### Runtime Paths
- Launcher MCP: `/Users/klyexy/.local/share/realm/mcp-server/realm-agent-launcher.py`
- Collaborator MCP: `/Users/klyexy/.local/share/realm/mcp-server/realm-collaborator.py`
- Launcher state: `/Users/klyexy/.local/share/realm-agent-launcher/agents/<agent_id>/`
- Codex MCP config: `/Users/klyexy/.codex/config.toml`
- OpenCode MCP config: `/Users/klyexy/.config/opencode/opencode.json`
- Cursor MCP config: `/Users/klyexy/.cursor/mcp.json`

### Task Wrapper Fix
- Patched `examples/opencode_realm_agent.py` so Realm `task.assign` runs use task-specific OpenCode session keys instead of reusing broad thread chat sessions.
- Task prompts now include structured task context: `task_id`, `title`, `thread_id`, and metadata.
- Task result delivery now uses `require_delivery_ack=False` to avoid false handler failures when the registry stores a result but the coordinator ack arrives late.

### Verification
- `tests/test_opencode_realm_agent.py`: `10 passed`.
- Restarted local agents `k-builder` and `k-reviewer`.
- Reran `realm_collaborator.collaborate_council` with `k-builder`, `k-reviewer`, and judge `realm-worker-a`.
- Result: both local agents completed the smoke task correctly, and the judge synthesized successfully.

---

**Note:** This file was misfiled at Realm repo root as `STATUS.md`. It is Stackwise content, not Realm OS status. Canonical tracker remains under `/Users/klyexy/Stackwise/`.
