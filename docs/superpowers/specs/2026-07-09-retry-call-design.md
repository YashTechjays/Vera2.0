# Retry Call — completed-but-incomplete retry + partial prompt + lineage (design)

**Date:** 2026-07-09
**Status:** Draft for review
**Phase:** 2 of 4 (Post-Call & Output). Builds on Phase 1 (Filled Form Eval).
**Owner:** yash@techjays.com
**Branch:** `feat/retry-call` (stacked on `feat/filled-form-eval` / PR #72, unmerged)

---

## 1. Context & problem

Phase 1 (Filled Form Eval) made a completed call's transcript into `field_answer(ai_call)`
rows with per-field confidence + a judge verdict, then routed the form to `COMPLETED` or
`EXCEPTION_REVIEW`. Today a completed-but-incomplete form (required fields still missing or
low-confidence) goes straight to human `EXCEPTION_REVIEW`.

The retry **plumbing already exists but is mostly unused**:
- `call.mode` = `FULL | RETRY`; the dispatcher already stamps `RETRY` when `retry_count > 0`.
- `patient_form.retry_count` + `tenant.max_retries`; the state machine increments on
  `CALL_FAILED → IN_QUEUE` (the existing **call-failure** auto-retry).
- `call_lineage(parent_call_id, retry_call_id)` table exists but is **never populated**.
- Dispatch **metadata** is the (non-PHI) channel to the DB-less worker; today it carries only
  the tenant `PersonaTweak` + optional IVR playbook. The worker's verification prompt is a
  **static** IBV script (`agent_worker/prompt.py`), not built per-form.

This phase adds the **completed-but-incomplete retry**: when the post-call eval finds required
fields unsatisfied and retries remain, re-queue the form for another call that **re-asks only
the unsatisfied fields**, link the attempt in `call_lineage`, and only fall through to
`EXCEPTION_REVIEW` when retries are exhausted.

### Approved decisions (from brainstorming)

1. **Trigger/precedence:** auto-retry when required fields are unsatisfied and retries remain;
   route to `EXCEPTION_REVIEW` only once retries are exhausted. Retry takes precedence over
   review while attempts remain.
2. **Unsatisfied includes low-confidence:** a required field is unsatisfied if unfilled **or**
   its `ai_call` value is judge-unsupported or **confidence < 70**. Threshold **70**.
3. **Partial prompt:** metadata nudge — pass unsatisfied-field **labels** (non-PHI schema
   metadata) in dispatch metadata; the worker focuses its base script on them.
4. **Lineage:** populate `call_lineage` at the single point where the dispatcher creates a
   `RETRY` call — covering **both** the call-failure and completed-but-incomplete retry paths.
5. **Worker prompt change is in scope** this phase.

---

## 2. The satisfaction / retry decision (replaces Phase 1's status decision)

Applied to the **required** schema fields (the same required set `completion_pct_v2` uses).

A required field is **satisfied** iff it has a current `field_answer` that is either:
- from a **trusted** source (`intake` / `human`) — trusted, not subject to confidence; or
- from `ai_call`, **judge-supported**, **confidence ≥ 70**, and **not token-valued**.

Then:
- **`retryable_required`** = required fields that are **askable** (DSL role `ask`/`confirm`) and
  **not satisfied** and **not token-valued**.
- **Token-valued fields** (any) still force `EXCEPTION_REVIEW` (Phase 1 safety, unchanged): they
  are *excluded* from `retryable_required` because re-asking would only re-tokenize them — a
  re-ask cannot fix a PHI-identifier value, so it is a review matter, not a retry.

Decision matrix (in `evaluate_call`, after persistence + completion-% recompute):

| Condition | Target |
|---|---|
| a token-valued field exists | `EXCEPTION_REVIEW` |
| `retryable_required` empty (all required satisfied) | `COMPLETED` |
| `retryable_required` non-empty **and** `retry_count < max_retries` | `AI_PROCESSING → IN_QUEUE` (retry) |
| `retryable_required` non-empty **and** retries exhausted | `EXCEPTION_REVIEW` |

The single confidence threshold is **`settings.post_call_review_floor`, bumped 60 → 70** and
reused as the required-field satisfaction bar (Phase 1's `needs_review` advisory uses the same
value — one source of truth, no second knob).

---

## 3. Missing-fields calc (shared, PHI-safe helper)

`retryable_required_paths(current_by_path, evals_by_path, schema_json) -> list[str]` in
`vera_core/forms/review.py`:
- Inputs: the form's current answers (path → {source, confidence, value-is-token?}), the latest
  `field_evaluation` per answer (supported/confidence), and the compiled schema.
- Output: the **field paths** (→ human labels resolved from the schema) that are
  `retryable_required` per §2. **Never values** — paths/labels only, so it is safe to compute in
  the dispatcher (which is PHI-free) and to place in dispatch metadata.

Used in **two** places, recomputed each time (never stored — avoids staleness when a human edits
a field between eval and dispatch):
- `evaluate_call` — to make the §2 decision.
- the dispatcher — to build the `retry_fields` metadata for a `RETRY` call.

Token detection reuses Phase 1's `has_phi_token`; the required-field set + label lookup reuse the
existing `completion_pct_v2` required-field logic (extract a small `required_paths(schema_json)`
+ `field_label(schema_json, path)` if not already present).

---

## 4. Partial prompt (metadata nudge)

- **Dispatcher:** when a candidate form is a retry (`retry_count > 0`, i.e. `call_mode == RETRY`),
  compute `retry_fields = retryable_required_paths(...)` (labels) and add them to the room
  metadata dict alongside the existing `PersonaTweak` fields. Labels are schema metadata
  (e.g. "network status", "specialist copay") — **non-PHI**. Cap the list length defensively.
- **Worker (`agent_worker`):** extend the metadata parse (`prompt.py` / `main.py`) to read
  `retry_fields`. `build_instructions` prepends a focus block when present:
  *"This call is a RETRY. The following data points are still missing — collect ONLY these and
  confirm them, then end the call: <labels>."* The base IBV script is otherwise unchanged; a
  missing/empty `retry_fields` leaves today's behavior exactly as-is (a `FULL` call).
- No PHI crosses: only field labels + the existing non-PHI tweak. The retry call runs a fresh
  room/transcript; its post-call eval extracts what was re-asked and the `is_current` merge makes
  those the current values while untouched fields keep their prior ones.

---

## 5. Lineage

Single insertion point in the dispatcher, right after a `RETRY` call row is created:
`session.add(CallLineage(tenant_id, parent_call_id=<most-recent prior call for the form>,
retry_call_id=<new call.id>))`. The parent is the latest `call` for the form by `created_at`
(before the new row). This covers **both** retry sources (call-failure and
completed-but-incomplete) because both re-enter `IN_QUEUE` and are dispatched the same way. A
`FULL` first call inserts no lineage row.

---

## 6. State machine

- Add edge `AI_PROCESSING → IN_QUEUE`.
- **Generalize the retry-cap guard:** currently `transition()` guards only
  `CALL_FAILED → IN_QUEUE` (checks `retry_count >= max_retries`, else increments). Change the
  guard to fire for **any** `target == IN_QUEUE` from a retry source `{CALL_FAILED,
  AI_PROCESSING}` — same cap check + `retry_count += 1`. So the cap is enforced identically on
  both paths and there is exactly one place that increments `retry_count`.
- `evaluate_call`'s retry branch calls `sm.transition(form, IN_QUEUE, ...)` inside
  `contextlib.suppress(InvalidTransitionError)` (mirroring the callback's failure path): if the
  cap is already hit the transition is refused and the form falls through to `EXCEPTION_REVIEW`.
  On success, set `form.enqueued_at = func.now()` (DB clock) so the dispatcher orders it.

---

## 7. Components (new & changed)

| File | Change |
|---|---|
| `vera_core/forms/review.py` | add `required_paths(schema_json)`, `field_label(schema_json, path)` (if absent), and `retryable_required_paths(...)`. |
| `vera_core/services/post_call_eval.py` | replace the §2 status decision: compute `retryable_required`, branch COMPLETED / retry(`IN_QUEUE`) / EXCEPTION_REVIEW; set `enqueued_at` on retry; audit `reason` (`retry` / `retries_exhausted` / existing). |
| `vera_core/services/form_state_machine.py` | add `AI_PROCESSING → IN_QUEUE`; generalize the retry-cap guard to `{CALL_FAILED, AI_PROCESSING} → IN_QUEUE`. |
| `vera_core/services/queue_dispatcher.py` | for a `RETRY` candidate: compute `retry_fields` labels → add to per-form metadata; after creating the call, insert `CallLineage`. (Metadata is currently computed once per pass; make the `retry_fields` part per-form.) |
| `vera_core/config/settings.py` | `post_call_review_floor` default `60 → 70`. |
| `agent_worker/prompt.py` + `agent_worker/main.py` | parse `retry_fields` from dispatch metadata; `build_instructions` prepends the retry-focus block when present. |
| `vera_core/schemas` (persona/metadata) | if dispatch metadata is a typed model, add optional `retry_fields: list[str]`; else it's a plain dict key. |

No schema/migration changes — all tables/columns exist (`call_lineage`, `call.mode`,
`retry_count`). Follow the idempotent-migration rules only if a column is unexpectedly needed.

---

## 8. Error handling & edge cases

- **Retry-cap** enforced in one place (state machine) → no infinite retry; exhaustion →
  `EXCEPTION_REVIEW`.
- **Nothing askable:** if required fields are unsatisfied but none are `ask`/`confirm` role (or
  all unsatisfied are token-valued), `retryable_required` is empty on the askable axis →
  `EXCEPTION_REVIEW`, never a pointless call.
- **Concurrent human edit:** `retry_fields` is recomputed at dispatch, so a field a human filled
  between eval and dispatch is not re-asked.
- **Idempotency:** unchanged from Phase 1 (redelivery guard on existing `ai_call` answers for the
  call); a retry is a *new* call with its own `call_id`, so its eval is a distinct job.
- **Metadata size:** cap `retry_fields` length; labels only, never values.

---

## 9. Testing

**Unit:**
- `retryable_required_paths`: unfilled required → included; low-confidence(<70) ai_call required
  → included; supported+conf≥70 → excluded; intake/human value → excluded (trusted);
  token-valued → excluded (goes to review); non-required or non-askable → excluded.
- extended `evaluate_call` decision matrix (COMPLETED / retry-IN_QUEUE / EXCEPTION_REVIEW incl.
  retries-exhausted and token-present).
- state machine: `AI_PROCESSING → IN_QUEUE` allowed + capped + increments; cap-exhausted refuses.
- worker `build_instructions` with/without `retry_fields`.
- dispatcher lineage-parent selection (most-recent prior call).

**Integration (docker Postgres + Redis, fake LLM):**
- eval on an incomplete form with retries remaining → form `IN_QUEUE`, `retry_count` bumped;
  dispatch creates a `mode=RETRY` call whose room metadata carries `retry_fields`, and a
  `call_lineage(parent, retry)` row exists.
- retries-exhausted incomplete form → `EXCEPTION_REVIEW`, no new call.
- a satisfied form → `COMPLETED`, no retry.

**Gate:** `just check` green → `/simplify` → re-check. New unit tests go under
`vera-backend/tests/unit/<area>/`, integration under `vera-backend/tests/integration/` (NOT
`packages/vera_core/tests/` — not in `testpaths`). Boot-verification not required (no new
long-lived loop; the dispatcher + consumer already exist).

---

## 10. Out of scope (later phases / deferred)

- **FE surfacing** of retry lineage / attempt history / confidence — Phase 3 (Dispute
  Resolution) territory.
- **AI_PROCESSING stuck-form reaper** — already deferred (`adr/devops-todo.md` #14).
- **Export** — Phase 4.
- **Dynamic schema-driven retry prompt** — the metadata-nudge approach is deliberate; a full
  per-form prompt rebuild is a future enhancement.

---

## Appendix — global constraints (inherited)

PHI never reaches the LLM raw / never in logs/metadata as values (labels/paths only); audit
records carry names/counts only; timestamps from the DB clock; all DB work inside a
tenant-scoped session; PEP 695 type params; asyncio only; `redis.asyncio` BLOCK reads raise
`TimeoutError`; migrations idempotent (none expected here).
