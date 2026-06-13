# Realm Agent Drills

**Purpose:** Test agent autonomy as small observable drills, not one-shot assumptions.

Each drill records:

- `drill_id`
- goal
- agents
- steps
- expected evidence
- observed evidence
- pass/fail
- fixes needed

---

## Drill Queue

| Drill | Goal | Status |
|---|---|---|
| `drill-discovery-001` | Prove agents can see teammates and roles | pass |
| `drill-pickup-001` | Prove task pickup emits ACK/WORKING/reply | pass |
| `drill-state-001` | Prove state files are readable and current | pass after fix |
| `drill-cancel-001` | Prove agents stop on cancel | partial |
| `drill-pickup-regression-002` | Prove pickup still works after Realm fixes | pass |
| `drill-review-loop-001` | Prove coder/reviewer loop | partial |
| `drill-restart-001` | Prove recovery after restart | pass |
| `drill-cancel-003` | Prove CANCEL task_id=X parsing is fixed | pending |

---

## Results

### `drill-discovery-001`

**Goal:** Prove each worker can identify itself, the teammate worker, and roles.

**Agents:** `eng-m2`, `m4-dl`

**Expected:** Each replies with `SELF`, `TEAMMATE`, and `STATUS=PASS`.

**Observed:**

- `eng-m2`: `SELF=eng-m2, primary coding agent on M2`; `TEAMMATE=m4-dl, reviewer/auditor on M4 Mac Mini`; `STATUS=PASS`.
- `m4-dl`: `SELF=@m4-dl, engineering agent on M4 Mac Mini`; `TEAMMATE=@eng-m2, engineering agent on M2`; `STATUS=PASS`.
- `realm_list_online` confirmed both worker agents online.

**Result:** PASS

**Notes:** `m4-dl` role answer was less specific than desired, but acceptable.

---

### `drill-pickup-001`

**Goal:** Prove request-kind tasks emit lifecycle messages and final replies.

**Agents:** `eng-m2`, `m4-dl`

**Expected:** ACK, WORKING, final reply with task ID.

**Observed:**

- `eng-m2` emitted `ACK task_id=08cbd7590c32406ea2ad07bb89b06ba2`, `WORKING task_id=08cbd7590c32406ea2ad07bb89b06ba2`, and final `PICKUP_OK eng-m2`.
- `m4-dl` emitted `ACK task_id=a36a624f74be4578a86d4bbdc71d6120`, `WORKING task_id=a36a624f74be4578a86d4bbdc71d6120`, and final `PICKUP_OK m4-dl`.

**Result:** PASS

**Notes:** Progress streaming leaked `[thinking]` from m4-dl into the thread during discovery. Useful for observability, but noisy. TUI should filter/collapse thinking/progress messages.

---

### `drill-state-001`

**Goal:** Prove network-readable state files reflect current worker status.

**Agents:** `eng-m2`, `m4-dl`

**Expected:** `get_agent_state eng-m2` and `get_agent_state m4-dl` return current JSON.

**Observed:**

- `get_agent_state eng-m2` returned valid state: `state=done`, `task_id=08cbd7590c32406ea2ad07bb89b06ba2`, `last_action=PICKUP_OK eng-m2`.
- `get_agent_state m4-dl` returned `no state file for m4-dl` at `/Users/klyexy/.local/share/m4-dl/state/m4-dl.json`.

**Result:** PARTIAL

**Fix needed:** `get_agent_state` is local-filesystem scoped. M2 MCP cannot read M4 state from M2 disk. Need remote-aware state lookup, replicated state, or route `get_agent_state m4-dl` to the M4 bridge.

### `drill-state-001` rerun after Realm commits `05cfed1` + `859ba3f`

**Expected:** `get_agent_state eng-m2` and `get_agent_state m4-dl` work from M2.

**Observed:**

- `get_agent_state eng-m2` returned local file state with `source=local_file`, `state=working`, `task_id=stackwise-backend-hardening-001`.
- `get_agent_state m4-dl` returned remote state via `source=agent_state_request`, `state=done`, `task_id=c430f158ec4e4eaa94a535a98ea4eb19`.

**Result:** PASS AFTER FIX

**Notes:** Required restart for M2 MCP/wrapper before fallback worked.

---

### `drill-cancel-001`

**Goal:** Prove agents can stop when a cancellation signal exists in the thread.

**Agents:** `eng-m2`, `m4-dl`

**Expected:** Agent reads thread, sees `CANCEL task_id=drill-cancel-001`, and replies `CANCELLED_OK <agent>`.

**Observed:**

- `eng-m2` found `CANCEL task_id=drill-cancel-001` and replied `CANCELLED_OK eng-m2`.
- `m4-dl` replied `CANCEL_NOT_SEEN m4-dl` even though a cancel message was sent in the same drill window.

**Result:** PARTIAL

**Fix needed:** Cancellation cannot rely on best-effort thread reads/race timing. The agent wrapper/runtime needs a cancellation registry or task lease state that is checked before and during work. Direct `CANCEL` messages should update task state centrally, not just appear as another thread message.

### `drill-cancel-002` rerun after Realm commits `05cfed1` + `859ba3f`

**Goal:** Prove direct `CANCEL task_id=X` request returns structured cancellation.

**Expected:** Both agents return structured cancelled status including `task_id=drill-cancel-002`.

**Observed:**

- `eng-m2` replied `{"type":"status","subtype":"cancelled","task_id":"","text":"Task cancelled"}`.
- `m4-dl` replied `{"type":"status","subtype":"cancelled","task_id":"","text":"Task cancelled"}`.

**Result:** PARTIAL AFTER FIX

**Fix still needed:** Cancel path now returns structured cancellation, but `task_id` is empty. The wrapper likely extracts `payload.task_id` from dict payloads and returns before parsing `payload.text`. For request payloads like `{"text":"CANCEL task_id=drill-cancel-002"}`, it must parse `task_id=` from the text when no explicit payload task_id exists.

---

### `drill-pickup-regression-002`

**Goal:** Prove ACK/WORKING/final reply still work after state/cancel/progress fixes.

**Agents:** `eng-m2`, `m4-dl`

**Expected:** ACK, structured WORKING/progress, final reply, state updated.

**Observed:**

- `m4-dl` returned `PICKUP_REGRESSION_OK m4-dl` and state `done`, `task_id=95245783e1e447b1b9648237b48f232f`, `source=agent_state_request`.
- `eng-m2` final reply appeared in the thread as `PICKUP_REGRESSION_OK eng-m2` and state updated to `done`, `task_id=5b2b5b99f22f43c6ae35cdcd491c41f3`, `source=local_file`.
- ACK/WORKING messages are now structured JSON strings with `type=progress`, `subtype=status`, `task_id`, `text`, and `visible_by_default`.

**Result:** PASS

**Notes:** The `realm_ask_text` call for `eng-m2` timed out client-side, but the final reply arrived and state updated. Treat as a client timeout/timing issue, not agent failure. TUI should parse JSON progress strings or Realm should send them as structured payload objects rather than JSON encoded in `text`.

---

### `drill-restart-001`

**Goal:** Prove agents can report their current task and planned next action after any restart/reconnection.

**Agents:** `eng-m2`, `m4-dl`

**Expected:** Each reads its state file and replies with current TASK/STATE/NEXT.

**Observed:**

- `eng-m2`: `TASK=stackwise-backend-hardening-001 STATE=notified`, `NEXT=reconstruct Stackwise context (STATUS.md, AGENT_TASKS.md, Realm thread) then continue backend-post-merge-hardening coding/review loop`.
- `m4-dl`: `TASK=625dbece162045b69704e09a828f78d8 STATE=working`, `NEXT=idle, awaiting next task`.

**Result:** PASS

**Notes:** eng-m2 correctly identified real task and recovery plan. m4-dl reported a stale task ID from a prior drill.

---

### `drill-review-loop-001`

**Goal:** Prove coder+reviewer loop: eng-m2 creates artifact, m4-dl reviews and reports findings.

**Agents:** `eng-m2` (coder), `m4-dl` (reviewer)

**Expected:** eng-m2 creates file with intentional typo, m4-dl reads and reports `FINDINGS=typo:testt` and `RATING=PASS`.

**Observed:**

- Step 1 (coder): eng-m2 created `/Users/klyexy/Stackwise/drill_test_review.md` with content `Drill review loop testt file - eng-m2 draft`. Reply: `FILE_CREATED`. File pushed to `backend-post-merge-hardening` branch.
- Step 2 (reviewer): First attempt: m4-dl couldn't find file via raw.githubusercontent.com (private repo, no auth). Reported `FINDINGS=drill_test_review.md not found, RATING=FAIL`.
- Step 2 retry with GitHub MCP tip: m4-dl emitted ACK+WORKING but is still processing (state=working at last check). The raw.githubusercontent.com URL is inaccessible without a PAT, but `github_get_file_contents` confirmed the file exists with the intentional typo.

**Result:** PARTIAL

**Fix needed:** Cross-machine code review requires artifacts to be pushed to a shared, authenticated channel (GitHub with PAT). Private repo raw URLs fail without auth. The reviewer (m4-dl) needs GitHub MCP access to the repo, or artifacts must be shared via a different mechanism (Realm blob, public gist, etc). Also notable: m4-dl's GitHub MCP tool visibility may be limited relative to eng-m2's full GitHub PAT.

### CANCEL Parsing Bug (from drill-cancel-002)

**Bug:** In `opencode_realm_agent.py`, `_extract_cancel_task_id` for dict payloads:
```python
if isinstance(payload, dict):
    t = str(payload.get("text") or "")
    return str(payload.get("task_id") or "").strip()
```
It returns `payload.task_id` immediately, ignoring `payload.text`. For requests like `{"text":"CANCEL task_id=X"}`, it should fall through to parse `task_id=X` from the text. The regex parsing branch only runs for string payloads. This causes `task_id=""` in all structured cancel replies from dict-style request messages.

**Fix:** Remove the early return for dict payloads, or chain: try `payload.get("task_id")` first, then fall through to text regex parsing.
