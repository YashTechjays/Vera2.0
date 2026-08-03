# Post-call eval ↔ Observer reconciliation — design

**Date:** 2026-07-27
**Status:** approved (brainstorm with Yash)
**Scope:** backend only — `vera_core/services/post_call_eval.py`, `EvalDeps` wiring in
`control_plane/post_call_consumer.py`, one new `ReviewReason` value, tests.

## Problem

Two features were built against the same data and never reconciled:

- **Post-call eval** (`evaluate_call`, commit `ef94f07`, Jul 9): re-reads the finished
  call's transcript with an LLM, persists `ai_call` field answers, judges each answer
  (`FieldEvaluation` rows), and decides the form's terminal status. Its step-1
  idempotency guard treats *any* `FieldAnswer(call_id=X, source='ai_call')` as proof
  the eval already processed call X.
- **Observer real-time extraction** (commit `4f0b8a9`, Jul 17): the agent worker
  extracts answers *during* the call; `worker_events._handle_call_answer_recorded`
  persists them as `ai_call` field answers with the live call's `call_id` — before
  `call.ended` is processed.

Consequence: on any call where the Observer captured at least one answer, the eval
consumer no-ops at the guard **without transitioning the form out of AI_PROCESSING**.
The judge never runs (no `FieldEvaluation` rows → `provenance.judge` null in tooltips
and exports), the decision table never runs, and the form strands until the pipeline
sweeper stamps `EXCEPTION_REVIEW / not_evaluated`. Verified end-to-end on
test.veratechsolutions.ai 2026-07-27 (form `019f8d7d-349f-7c13-b2d7-22ced510b0e0`:
119 Observer-written answers, 0 judge verdicts, reason `not_evaluated`).

Secondary defects fixed in the same batch:

- The eval's requeue branch does not check the tenant `form_auto_retry_enabled`
  setting (the fallback resolver does), so enabling the eval would auto-redial payers
  for tenants that disabled auto-retry.
- Step 6 silently drops judge verdicts whose `field_path` matches no kept answer —
  no log, indistinguishable from "judge never ran".

## Decision (from brainstorm)

**Judge + top-up** semantics, implemented by **removing the answer-existence guard**
(approach A):

1. Keep the Observer's live answers as-is — they are the extraction for whatever the
   call covered.
2. The eval judges every current `ai_call` answer written by *this* call, extracts
   only the still-missing fields from the transcript, persists those, judges them too
   (one combined judge pass), then runs the existing decision step.
3. Requeue is gated on `form_auto_retry_enabled`.

## Design

### 1. `evaluate_call` body (post_call_eval.py)

Guards unchanged: status guard (`!= AI_PROCESSING` → no-op), no-transcript route,
schema-parse route. **Delete the answer-existence idempotency guard** (lines 140–152).
Then:

1. **Load Observer answers**: current `FieldAnswer` rows for
   `(form_id, call_id == this call, source == 'ai_call', is_current)`. These carry
   `value`, `confidence`, `evidence_seq`.
2. **Top-up extraction**: `missing = doc.collection_paths() − {paths with any current
   answer (any source)}` — a human/intake/prior-attempt answer is not missing. If
   `missing` is non-empty: `deps.llm.extract(field_paths=missing, turns=turns)`, then
   the existing persist flow (PHI-token filter → last-occurrence dedupe → batch demote
   → insert, flush). Extract failure keeps the existing `LLM_ERROR` routing.
3. **Single judge pass** over both batches. Observer answers and newly extracted
   fields both reduce to the `field_path / value / evidence_seq` shape
   `build_judge_prompt` already takes. Judge failure keeps the `LLM_ERROR` routing.
4. **Write `FieldEvaluation`** rows keyed by each batch's `answer_id` (Observer rows
   were loaded; new rows have client-minted uuid7 ids).
5. **Decision** (existing steps 7–12) unchanged apart from retry gating: recompute
   `completion_pct`, write snapshot `after_state`, run the satisfaction check over
   *all* current answers, transition, audit, dispatch. This restores real review
   reasons (`ready_for_review`, `retries_exhausted`, `unsatisfied_unaskable`, …).

### 2. Idempotency

The transaction already provides it: a committed eval transitioned the form out of
`AI_PROCESSING` (redelivered jobs no-op at the status guard); a rolled-back eval left
no partial state (re-run from scratch is correct). The answer-existence guard was
redundant belt-and-braces whose core assumption ("`ai_call` answer ⇒ eval ran") the
Observer feature falsified. It is removed, not patched.

### 3. Retry gating

- `EvalDeps` gains `auto_retry_enabled: bool = False`.
- `post_call_consumer` passes `settings.form_auto_retry_enabled` (same flag the
  fallback resolver already uses).
- Requeue branch becomes `if retryable and deps.auto_retry_enabled and
  sm.can_retry(...)`.
- New `ReviewReason.AUTO_RETRY_DISABLED = "auto_retry_disabled"` for the
  retryable-but-gated-off route. `StrEnum` + `String(32)` column → **no migration**;
  the frontend Reason chip renders via the generic `statusLabel()` humanizer → **no
  frontend change**. `user_ended` still wins over requeue.

### 4. Observability

In the verdict-matching loop, `logger.warning` when (a) a verdict's `field_path`
matches no kept answer or (b) a kept answer received no verdict — counts plus the
mismatched **paths only** (never values; paths are schema constants, not PHI).

### 5. Testing (extend existing `evaluate_call` unit tests / fake LLM)

- **Regression for the bug**: Observer answers pre-seeded for the call → eval judges
  them (FieldEvaluation rows exist), extracts only missing paths (assert the fake's
  `field_paths` argument), transitions with a real reason — not a no-op, no
  `not_evaluated`.
- Nothing missing → no extract call; judge-only.
- Retryable + flag off → `EXCEPTION_REVIEW / auto_retry_disabled`; flag on →
  `IN_QUEUE`; `user_ended` overrides.
- Redelivery after success → status-guard no-op (existing behavior stays green).
- Verdict path mismatch → warning logged, remaining verdicts still written.

## Out of scope

- `VERA_GCP_PROJECT` unset on the test environment (deploy/infra task — the eval
  consumer cannot start without it; tracked with the team).
- Live in-call form projection showing 0% during the call (separate worker/UI issue).
- Live Monitoring UI, exports, frontend — no changes required by this design.

## Verification

`just check` (ruff + mypy --strict + pytest) on the exact final tree; `/simplify`
pass after implementation, then `just check` again. No service-boot verification
needed: no background-loop changes (the consumer loop itself is untouched).
