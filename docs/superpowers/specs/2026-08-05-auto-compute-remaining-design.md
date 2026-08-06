# Auto-computed Remaining for money triplets — design

**Date:** 2026-08-05
**Status:** Approved
**Depends on:** the `NumericConsistency` machinery from
`docs/superpowers/specs/2026-08-04-lifetime-max-numeric-consistency-design.md`
(branch `fix/lifetime-max-consistency`, PR pending). This work stacks on that branch.

## Problem

QA report: realtime form filling never calculates Remaining for the money triplets
(Deductibles individual/family, Out-of-Pocket Maximums individual/family, Lifetime
Maximum). Remaining is filled only when the rep literally states it; the system never
computes Total − Met, so the field sits empty in the live form and review UI even when
both inputs are known. No derivation mechanism exists anywhere: the DSL `Derive` is a
prompt-only instruction (condition → fixed string), the Observer records only what was
spoken, the frontend only displays.

## Decisions (from brainstorming)

1. **Scope / coupling:** all 5 triplets. The 4 missing `NumericConsistency` rules are
   authored (deductibles individual/family, OOP individual/family) and the same five
   declarations drive BOTH consistency validation and derivation. Accepted side effect:
   deductibles/OOP get the live consistency ReAsk too (closes the remainder of QA's
   original validation report).
2. **Overwrite policy — fill gaps only:** compute only while Remaining is empty or
   still holds the value we ourselves last derived. A rep-stated Remaining always wins
   and permanently stops derivation for that field (if it conflicts with the math, the
   existing ReAsk challenges it). Supervisor manual edits stay pinned exactly as today
   (`editedPaths` in the frontend, HUMAN supersede in the DB).
3. **Agent behavior unchanged:** the agent still asks its scripted Remaining question
   mid-call (the rep's answer is a cross-check and overwrites the computed value). The
   computed answer silences only the snapshot-driven gates — the end-of-call gap pass
   and the premature-completion refusal — because it lands in the worker run-state.
4. **Placement — Observer-side (agent worker):** the only placement that feeds the live
   run-state snapshot (gap-pass suppression) AND rides the existing
   `record_answer → CallAnswerRecordedEvent → SSE → FormValues` pipeline, so
   persistence, live UI updates, validation, and human-override protection all come
   for free. Rejected: control-plane consumer (worker never learns the value → gap
   pass still re-asks), frontend-only (nothing persisted).

## Derivation semantics

For each `NumericConsistency` rule in the CallPlan, whenever an answer is recorded for
the rule's `<triplet>.total` or `<triplet>.met_amount`:

- Parse both inputs with the existing `parse_currency` (strip `$ , %` whitespace;
  specials like "No Limit"/"Unlimited"/prose → do not parse).
- Compute only when both parse and `0 ≤ met ≤ total`. Met > Total is left to the
  existing ReAsk (inconsistent inputs must not silently produce a value).
- Write only when the current Remaining is blank OR equals the value this run last
  derived for that path (per-run map `path → last derived value`); recompute when
  Total/Met are corrected later. Once the rep (or anything else) supplies a different
  value, derivation for that path stops for the rest of the run.
- Value stored as `f"${total - met:,.2f}"` (e.g. `$20,000.00`) — parses cleanly on
  both sides of the validation parity pair.
- Confidence and `evidence_seq` are inherited from the triggering input answer
  (monotonic-seq replay guard in the DB writer stays satisfied). Source remains
  `ai_call` — no new enum, no migration; disputes and human supersede behave as for
  any observer answer.
- The write goes through the same locked recording routine as extracted answers
  (idempotent: unchanged values early-return, so no event storms), emits the normal
  `CallAnswerRecordedEvent`, updates the controller snapshot, and is evaluated by the
  rule engine like any answer (a derived value is consistent by construction and can
  never fire the triplet's own ReAsk).

## Components

1. **`vera_core/forms/consistency.py`** — new pure helper:
   `derive_remaining(total_raw: str, met_raw: str) -> str | None` (parse, bounds
   check, format). Stdlib-only module unchanged in character; unit-testable alone.
2. **`agent_worker/observer.py`** — `ObserverManager` learns the plan's triplet rules
   (`(rule, total_path, met_path, remaining_path)` precomputed at construction from
   `plan.numeric_consistencies`), a `_derived: dict[str, str]` map, and a derivation
   step at the end of the locked record path: when the recorded `field_path` is a
   rule's total/met input, attempt `derive_remaining` over the current snapshot values
   and record the result via the same internal routine (lock already held — no
   re-entrant locking).
3. **`vera_core/forms/catalog/ibv_standard.py`** — author the 4 new rules with
   per-triplet clarify texts; `just compile-schemas` regenerates the artifact
   (never hand-edited); `just seed-schemas` publishes a new schema version.
4. **Frontend — no changes.** The live modal and review UI render whatever lands in
   `FormValues`; `validateAll`'s numeric-consistency pass accepts a derived value by
   construction and now also covers the 4 newly declared triplets via the artifact.
5. **Control plane / DB — no changes.** Derived answers are ordinary `ai_call`
   `FieldAnswer` writes via the existing consumer.

## Edge cases

- **Rep states Remaining first, Total/Met arrive later:** Remaining non-blank and not
  ours → never overwritten.
- **Total or Met corrected after we derived:** current Remaining equals our last
  derived value → recompute with the corrected inputs (and the unchanged-value
  early-return suppresses no-op rewrites).
- **Total is $0 / "None" (deductible no-ops):** Met is schema-gated off; without a
  parsed Met nothing is derived. If both are genuinely $0, derived `$0.00` is correct.
- **"No Limit"/"Unlimited" totals:** don't parse → no derivation (Met/Remaining are
  gated off anyway).
- **Met > Total:** no derivation; the consistency ReAsk (now on all 5 triplets)
  challenges the rep.
- **Supervisor edits Remaining mid-call:** frontend `editedPaths` blocks live
  overwrites in the UI, and the derived value differs from the human DB row only
  until the human supersede lands — same semantics as any ai_call answer today.
- **Retry/focused calls:** derivation reads the run's own snapshot; prefilled answers
  from a prior attempt count as "non-blank" and are not overwritten.

## Out of scope

- Marking computed values differently in the UI (provenance badge) — not requested.
- Skipping the agent's Remaining question when computable (prompt change; rejected in
  brainstorming).
- Arithmetic in the DSL `Derive` construct.
- Any new `AnswerSource` enum member.

## Testing & verification

- **Unit (`consistency.py`):** `derive_remaining` — formatting (`$20,000.00`),
  zero-total, met==total → `$0.00`, met>total → None, unparseable/special inputs →
  None, negative met → None.
- **Observer tests (`test_observer.py`):** derives when total+met recorded (either
  order); fills only blank Remaining; recomputes after a corrected input; stops after
  a rep-stated Remaining; emits exactly one event per new value (idempotency); gap
  pass sees the field as answered.
- **Rule interplay (`test_rule_engine.py`):** a derived-value snapshot never fires the
  triplet's ReAsk.
- **Catalog:** freshness + carry tests updated for 5 rules; per-triplet clarify texts.
- **Gates:** backend `just check`; frontend four gates (artifact its tests import
  changes).
- **Manual pre-ship:** eval-harness scenario + a live call (observer/voice-path
  change), as with the parent branch.
