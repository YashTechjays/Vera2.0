# Retry-call rework — plan index

**Date:** 2026-08-21
**Branch context:** `fix/retry-calls`
**Spec:** `vera-backend/docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md`

A focused retry re-asks every question; the operator surface never produces a retry at all; and the
retry ask set trusts intake values the payer never confirmed. The spec has the measurements. This
splits the fix into plans that can each be executed by an independent session.

## Plans and dependency order

| plan | file | depends on | ships alone? |
| --- | --- | --- | --- |
| **A** — `collected_per` DSL marker | `2026-08-21-a-collected-per-dsl-marker.md` | — | **yes** (inert until B) |
| **B** — authoritative focus set + reference-number gate | `2026-08-21-b-focus-set-and-gate.md` | A | yes |
| **C** — question-tree narrowing on a focused retry (P7) | `2026-08-21-c-focused-retry-narrowing.md` | A, B | yes |
| **D** — both percentages count only what a call can fill | `2026-08-21-d-call-scoped-percentages.md` | B | yes |
| **E** — dispute suppression + per-attempt form view | `2026-08-21-e-per-attempt-view.md` | A, B | yes |

**A is the foundation and is inert on its own** — it adds vocabulary, marks both catalogs, and
re-seeds; nothing reads the marker until B. Its acceptance test is that the seeded scenario's focus
counts are UNCHANGED afterwards.

**B and C are the retry fix** and should ship together or in immediate succession. B puts the right
paths in the focus set; C makes the agent *speak* only those. **B alone changes which fields are
tracked without changing what is asked** — i.e. it reproduces today's P7 defect over a wider set of
calls. Do not stop at B.

**D is required for B to have an effect in the auto-retry path.** Until it lands, a
non-authoritative call's answers still raise `verified_fraction`, which can park a form as
`FILL_THRESHOLD_MET` so the retry B would have focused is never dispatched (spec D9). B+C are still
worth shipping without D — the operator-triggered path works — but the automatic path is not fixed
until D.

**E** is what makes the marker earn its keep beyond retry: it suppresses the permanent
`previous_value: null` disputes on call-scoped fields, flags non-authoritative answers so a reviewer
can see them for what they are, and adds the per-attempt view.

## Scope of each plan

### A — `collected_per` DSL marker

`collected_per: CollectedPer | None = None` on `Leaf`, `Group` and `Section` (`None` = inherit,
document default `"form"`, so most-specific-wins works); a resolution helper applying that
inheritance to `ask`-role leaves; a `Leaf` validator restricting `"call"` to `role="ask"`. Three
marks per catalog — section-level on both representative sections and on `patient_verification`,
leaf-level on `coverage_summary.disease_coverage_active` (that section also holds form facts), which
means threading `collected_per` through `enum_ask`. A catalog test that the reference-number and
rep-name leaves resolve to `"call"`, plus the **intro tripwire**: a task carrying the call's opening
speech must be retained by the derived retention rule (vacuous where the opening task has no
`intro`). Then `just compile-schemas`, `just seed-schemas`, and a re-seed of every form — there is
no backfill migration, because there is no production data (spec D7).

**Produces:** `FormSchemaDoc.collected_per_call_paths() -> frozenset[str]`.

### B — authoritative focus set + reference-number gate

- `is_call_confirmed(status, *, authoritative_calls, floor)` beside `is_field_satisfied`, which is
  **not** modified (spec D8). `FieldStatus` gains `call_id` (appended with a default, so the many
  three-argument constructions in existing tests keep working); `load_field_status` selects it; and
  a new `load_authoritative_call_ids(session, form_id, *, reference_field)` resolves the set
  (`SELECT DISTINCT call_id … WHERE field_path = <ref field>`, **no** `is_current` filter).
- `_required_paths(..., include_defaulted: bool = False)`; the ask set passes `True`, so the retry
  stops skipping the seven `default: "N/A"` leaves a fresh call already asks — `owed_now`'s
  "`default` is deliberately not consulted" applied to the retry side.
- `focus_paths(doc, status_by_path, schema_json, *, floor, values, authoritative_calls)`
  composing: not `is_call_confirmed` ∪ group expansion ∪ `collected_per_call_paths()`.
- Focus gated on the captured reference number alone; the `call_mode == RETRY` precondition goes.
  `call.mode` still records RETRY vs FULL from `retry_count` for reporting and `CallLineage`.
- **Deletes `bookend_paths` and its tests.** Nothing replaces it: this branch requires the schema
  version Plan A publishes (spec D7). A pre-marker document is covered by Plan A's re-seed check, a
  schema authored without the marker by Plan A's catalog tests.
- Task retention (spec D2) needs **no code of its own**: because `collected_per="call"` paths are
  always in the focus set, the greeting and wrap-up tasks always have kept fields, so
  `focus_call_plan`'s existing "drop a task with no kept fields" rule retains them. Likewise
  `plan_runtime.py` is untouched — `_skip_when_nothing_applies` already returns early on
  non-empty `applicable_fields`, and `_closing_task_index = len(plan.tasks) - 1` still lands on
  `wrap_up` now that it is guaranteed retained. The one uncovered case (a task that collects
  nothing at all, which the DSL permits and `focus_call_plan` drops) is handled in Plan C.

**Produces:** `focus_paths(...) -> list[str]`, `is_call_confirmed(...)`, `FieldStatus.call_id`,
`load_authoritative_call_ids(...)`.

**Acceptance:** on a Family plan the askable required set goes 40 → 47 (defaults included); an
intake-supplied askable leaf becomes owed; an answer from a non-authoritative call becomes owed.
The 72 gate-dependents of an unanswered parent are **not** recovered by B — `_required_paths` still
filters on `is_applicable`, so that is Plan C's `explode`.

### C — question-tree narrowing on a focused retry

`focus_call_plan` gains the doc and the answer map, narrows `panels` with
`focus_questions(..., explode=True)` — the same primitive and flag the gap pass already uses — and
re-renders `lead_in` / question list / completeness block / `trailing` the way
`PlanTaskAgent._assembled_block` already does. **`fields` must be derived from the target paths of
the kept questions**, not from the input path set: `explode` adds questions, and a rendered question
whose fields are absent from `task.fields` is invisible to `owed_now`, so the refusal and gap pass
would not track it. That fields/panels agreement is the invariant today's code breaks. This is P7 /
Plan D of the prompt-compiler overhaul (`2026-08-06-d-focused-retry-prompt-narrowing.md`).

**Acceptance:** the spec's table moves — `diagnostic_coverage` 4 spoken questions → 1, `financial`
18 → 10, prompt chars no longer byte-identical to the unfocused plan. And with
`infertility_tx_covered` unanswered, its dependents appear in the rendered list rather than leaving
the agent with one question and nothing sanctioned to follow it with.

### D — both percentages count only what a call can fill

Only `ask`/`confirm` leaves are fillable by a call, so both percentages restrict their denominator
to those (spec D9):

- `satisfied_required_fraction` (→ `verified_pct` + the park gate): `is_call_confirmed` **and**
  `askable_only=True` — either alone is wrong, since authoritative-only against the current
  denominator caps `verified_pct` at 90.9% and makes a high `retry_fill_threshold` unreachable.
- `completion_pct_v2`: `askable_only=True`. All 15 non-askable required leaves are
  `required_intake_fields`, hence always filled, hence a constant 30.6% offset (21.4% for
  disease-only) that no call can move — and it gates the `low_fill` retry decision in
  `post_call.py:93`.

Both move together because `post_call.py` and `post_call_eval.py:511` compare *different* numbers
against the *same* `tenant.retry_fill_threshold`. `unsatisfied_required_paths` is untouched — for
human sign-off, an intake-supplied patient name IS satisfied.

Carried along: the frontend mirror `completionPercent` (`vera-frontend/src/lib/ibv/schema.ts:316`),
`worker_events.py`'s per-call SSE payload, the worklist projection, a recompute of stored
`completion_pct` and `verified_pct`, and a note that `retry_fill_threshold` wants revisiting now that
both gates finally measure the same population.

**Acceptance:** seeded form `verified_pct` 92.68% → 91.95%; a brand-new IBV form reads 0% complete
rather than 30.6%; a form whose values came only from a non-authoritative call reports 0% verified
and is not parked as `FILL_THRESHOLD_MET`.

### E — dispute suppression + per-attempt form view

`build_field_views` takes the call-scoped path set and skips `dispute_view` for those paths (spec
D6). `load_field_provenance` gains `authoritative` so the review surface can mark a
non-authoritative call's answers unverified while still showing them (spec D9). Then the
per-attempt view: what each call collected (`field_answer.call_id` + `load_field_provenance`, both
already present) and the before/after diff (`CallFormSnapshot`, already populated — verified 169
keys each way on the observed call). Must treat `after_state == {}` as "never finalized", distinct
from "nothing changed".

## Shared facts every plan needs

- Every command runs from `vera-backend/`.
- `vera_core/forms/` stays pure and DB-free; the worker has no `FormSchemaDoc` at runtime.
- **No `dsl_version` bump** (the field is optional), **no backfill migration, and no runtime
  fallback** — there is no production data, so this branch simply requires the schema version Plan A
  publishes (spec D7). Two guards carry it, both in Plan A: the re-seed check (no `patient_form`
  pinned to a demoted version) and the catalog tests (a schema authored without the marker fails
  CI).
- **Do not add a document-level validator requiring the marker** — combined with the `role="ask"`
  restriction it makes ~12 existing fixtures unconstructible (two point
  `rep_call_reference_number_field` at a `context`-role leaf). The guard is a catalog test (spec D1).
- **`Call.call_reference_no` is dead** — nothing in the pipeline writes it. Authoritative-ness comes
  from `field_answer`, never that column (spec D8). Plan A also drops the seed script's write to it.
- `is_field_satisfied` is **not** modified by any plan, and `unsatisfied_required_paths` keeps
  today's behaviour (human sign-off legitimately trusts intake). Both percentages change in D.
- Never log a field value.
- `just check` verbatim, then `/simplify`, then `just check` again, before claiming any plan done.
- **Spoken behaviour is not verified by pytest.** After C, run the eval harness and a live
  browser-callee retry:
  ```bash
  VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
  ```
  `-m evals` is required; confirm real scenarios ran by the `===== <scenario>: … =====` banners.

## Reproducing the scenario

`just seed-retry-form` seeds the form these measurements came from: one prior COMPLETED authoritative
call with reference `8842-QX-77`, 152 judge-supported `ai_call` answers, `deductibles` /
`out_of_pocket` / `lifetime_maximum` never reached, and both members of one cost-share either/or pair
judge-rejected. It prints the scope a correct focused retry would ask (12 owed → 42 after group
expansion). `just arm-retry-form` moves it to `IN_QUEUE` preserving `retry_count`, which is the only
way to exercise the focused path before B lands.

Three gaps in that seed to close while working here: it writes no `CallFormSnapshot` row for attempt
1 (Plan E's view shows attempt 1 as snapshot-less), it writes `Call.call_reference_no` which nothing
reads, and its `--missing` default leaves two cases unexercised that B and C need — a form where an
`applicable_when` parent is itself unanswered, and a form carrying a **non-authoritative** attempt
(answers with a `call_id` whose call captured no reference number).

## Open item, deliberately not planned

**Should an intake value satisfy a confirm-role leaf for the READY_FOR_REVIEW gate?**
`unsatisfied_required_paths` keeps `is_field_satisfied`, so a form whose member ID came from intake
and was never read back to the payer can still route to `READY_FOR_REVIEW` — "nothing is wrong, just
sign it off". D8 fixes this for the retry ask set and D9 for `verified_pct`, but the human sign-off
gate still trusts intake. Left alone deliberately: it is a compliance-flavoured decision about what
a reviewer is being told, not a retry defect.
