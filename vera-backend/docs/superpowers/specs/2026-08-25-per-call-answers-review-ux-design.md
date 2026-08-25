# Per-call answers, disputes and the retry decision — review-surface design

**Date:** 2026-08-25
**Branch context:** `fix/retry-calls`, after the 40-commit retry rework landed
(`docs/superpowers/reviews/2026-08-21-retry-rework-post-implementation-review.md`)

The retry rework settled **what a focused retry asks** — verified live at 16 questions
(review §15.1). It left two things unsettled, and a product owner hit both on call
`01a037af-c077-7af0-b266-5587eca6384a` (form `TEST-SEED-RETRY`):

1. a per-call fact (rep name, call reference number) changed between attempts with no
   surface saying so — correct by spec D6, illegible on screen;
2. a retry re-asked a whole 32-member panel because two of its leaves were judge-rejected —
   correct group closure, and nothing on screen explains it.

This spec covers the review surface for both, and the **retry initiation decision** behind
them, which turned out to be two gates comparing one tenant knob against different numbers.

## Shape of the work

Two implementation plans, in this order, not one:

1. **Backend (B1–B8).** Lands the truth the UI needs, and the only behaviour changes in the
   branch. B5's DTO fields and B8's floor are prerequisites for F0/F4, so this goes first.
   B6 and B7 change which forms redial live payers — hence the live merge gate below.
2. **Frontend (F0–F5).** Pure display on top of B5. F0 is a prerequisite for F1–F5.

Splitting them keeps the live-verification surface (B6/B7) reviewable on its own instead of
entangled with six UI parts.

---

## Evidence

Every number below was derived from the compiled artifact
(`data/form_schemas/ibv_form_standard_v2.json`) or measured against the source in this tree.
Per review §12, no constant here was pattern-matched from a neighbouring section.

### E1. Leaf population

```
roles: ask 179, context 17, input 5, confirm 3        (204 leaves total)
collectable (ask|confirm) = 182
  in `ui.layout: "table"` sections   127   (70%)
  in field-row sections               55
required_intake_fields              16
```

`labs_xray_ultrasound` is a **group inside** the `diagnostic_testing` table section:
8 CPT sub-groups x 4 collectable leaves = **32**, all `role: ask`. A rejected leaf in
`cpt_58340` closes both the 4-leaf CPT group and the 32-leaf panel via `expand_to_groups`,
so **32 is the number a reviewer perceives**.

### E2. Four defects in the current review surface

| # | defect | where |
|---|---|---|
| 1 | `provenance` is unreachable except through a dispute. `provenanceFor` has three consumers: the `authoritative === false` pill, and `DisputeTooltipBody` in `FieldRow`/`SectionMatrix` — which renders only while `showDispute` holds. Attempt, mode, judge verdict and evidence are fetched, typed, and never shown for a non-disputed answer. | `FieldRow.tsx:47,83`, `SectionMatrix.tsx:114` |
| 2 | `SectionMatrix` renders **no** "Unverified" pill. 127 of 182 collectable leaves (70%) live in table sections, so for most of the form the field-level marker is not drawn at all. | `SectionMatrix.tsx` |
| 3 | The judge-rejection tint is gated on the dispute: `highlightClass = showDispute ? … : undefined`. A rejected answer with no dispute (call-scoped path, or AI value equal to baseline) gets zero weight, and the tint disappears the moment the dispute is applied. | `FieldRow.tsx:52`, `SectionMatrix.tsx:119` |
| 4 | Bulk accept discloses nothing. The section pill says "12 disputes" and resolves them, never how many are unverified or judge-rejected — and a collapsed section's pill still accepts unseen fields. | `Section.tsx` |

### E3. The retry decision is two gates on one knob

| path | runs when | gate | population |
|---|---|---|---|
| `post_call_eval.py:~520` | the eval consumer is configured | `verified_fraction >= tenant.retry_fill_threshold` | **34** — askable, defaults excluded, requires an authoritative call (`is_call_confirmed`) |
| `post_call.py:93` | eval consumer absent, or the sweeper reclaims a stranded form | `form.completion_pct < retry_fill_threshold * 100` | **39** — askable, defaults counted *filled*, trusts intake |

Measured on the artifact:

| scenario | `completion_pct` (n=39) | `verified_pct` (n=34) | unsat | retryable | ask set |
|---|---|---|---|---|---|
| S1 never dialed, intake only | 15.38% | 0.00% | 33 | 33 | 48 |
| S2 good call, reference captured | 100.00% | 100.00% | 0 | 0 | 3 |
| **S3 identical call, no reference number** | **100.00%** | **0.00%** | 0 | 0 | 48 |
| S4 authoritative, 4 leaves judge-rejected | 100.00% | 88.24% | 4 | 4 | 6 |
| S5 authoritative, answered 24 of 39 | 64.10% | 58.82% | 14 | 14 | 18 |

**S4 at T=0.90 — the two gates disagree on identical data:**

```
eval-path:     verified 88.24% < 90  ->  RETRY
fallback-path: completion 100%  >= 90 ->  park (no low_fill)
```

Which one runs depends on whether the eval consumer was configured, not on the data.

**S3 is the semantic argument.** Same call, same answers, same instant; the rep simply never
gave a reference number, so nothing ties any of it to a payer-side record. `completion_pct`
says the form is finished, `verified_pct` says nothing has been proven.

> S1 reads 15.38% here where review §7.3/§14.1 measured ~44.68% on a seeded form. Not a
> contradiction: S1 fills exactly the 16 `required_intake_fields`, the seeded form fills more,
> and `completion_pct_v2`'s denominator moves with applicability. Both are correct measurements
> of different inputs. The gate arithmetic below does not depend on either figure.

### E4. None of the decision inputs reach the browser

`PatientFormDetail` (`patient_forms.py:600`) carries `completion_pct` and nothing else.
Absent: **`verified_pct`** (computed, persisted, *is the gate*, never leaves the database —
it appears nowhere in the patient-forms API), **`review_reason`** (on
`PatientFormSummary` only, so the worklist chip shows it and the review modal does not),
**`retry_count` / `max_retries`**. No UI can explain the retry decision however well designed.

### E5. The confidence bands contradict the floor

`disputes.ts` bands at 95/85/75 have no relation to `REVIEW_CONFIDENCE_FLOOR = 70`
(overridable by `settings.post_call_review_floor`, `VERA_POST_CALL_REVIEW_FLOOR`). A
judge-supported answer at **72** clears the floor — the pipeline counts it confirmed and will
never re-ask it — and `confidenceLevel` puts it in `very-low`, which
`confidenceHighlightClass` paints red with a red ring. The same red a judge-*rejected* answer
at 38 gets. A four-step gradient implies a continuum no decision in the system uses.

### E6. The floor is not wired on the dispatch path

`dispatch.py:149` never passes `retry_floor`, so production takes the module default (70)
while `post_call_consumer.py:99` takes `settings.post_call_review_floor` from `main.py:340`.
Harmless before this branch — `retry_floor` only chose which field labels to embed in room
metadata, which is exactly what its docstring still claims:

> *"Best-effort prompt guidance only — the authoritative retry-vs-review decision happened
> earlier in `evaluate_call`."*

That is now false: Plan B wired the same parameter into `focus_paths(floor=retry_floor)` at
`:418`, so it selects the **retry ask set**. With `VERA_POST_CALL_REVIEW_FLOOR=85`, a field at
confidence 78 is counted unsatisfied by `post_call_eval` (so it triggers the retry) and
confirmed by the dispatcher's focus set (so it is dropped from the ask set) — **the retry
dials and never asks the question that caused it.** Latent only because the var is unset.

### E7. The export would misstate the flag if the column were added naively

`build_workbook` already writes a Provenance sheet with `Attempt | Mode | Judge confidence |
Supported`, and the endpoint already loads `attempts` and `prov`. It calls both loaders
without `authoritative_calls`, so per `_authoritative` **every row's `authoritative` defaults
to `True`** — including the unverified ones.

### E8. `default` is the DSL's payer-refusal escape hatch

`spouse_partner_name` and `spouse_partner_dob` are `role: confirm`, required under
`family_coverage`, and both declare **`default: "N/A"`**. `_required_paths` drops any
defaulted leaf unless `include_defaulted=True`, so measured:

```
coverage_type flips to Family mid-call, spouse details never disclosed (HIPAA):
  spouse_name   gate population False   unsatisfied False   retryable False   ask set True
  spouse_dob    gate population False   unsatisfied False   retryable False   ask set True
  policy_number gate population True    unsatisfied True    retryable True    ask set True
```

Identical whether intake typed `"N/A"` or left it blank. So a required-ness gate that flips
mid-call cannot point the retry loop at data the payer will not disclose — the declared
default absorbs it (spec §4.4), and the leaf is still asked once per retry, never looped.
**`policy_number` is the only confirm leaf with no default**, so §4.1 reduces to it alone.

---

## Design — frontend

### F0. Shared foundation

New pure module `src/lib/ibv/provenanceView.ts`:

- `answeredNotCounted(provenance, floor)` — the central predicate: provenance exists **and**
  the answer is not call-confirmed. Three causes: judge-rejected (`judge.supported === false`),
  below the floor, or `authoritative === false`. This is "the rep told us and the system still
  owes the question" — deliberately **not** "never confirmed", which is the normal state of an
  intake-supplied answer (S1 measures 0% verified) and would light up nearly every group.
- `groupClosures(schema, provenanceFor, floor)` — per offending leaf, the **outermost**
  enclosing group and its collectable-leaf count.
- `sectionRisk(schema, sectionKey, …)` — `{disputes, unverified, rejected}`.
- `recollectedPaths(attempts)` — paths appearing in >= 2 **finalized** attempts'
  `changed_paths`.

`attempts` moves into `IbvProvider`, fetched alongside the detail, so both tabs read one
fetch (today `CallHistoryTab` fetches locally and the Form tab cannot see it).

The only new backend mirror is the `role in {ask, confirm}` collectable filter, which
`completionPercent` already relies on. Group membership, `isApplicable`, `isRequired` and
`alternativeSiblings` all exist in `schema.ts`. **`focus_paths` is deliberately not
mirrored** — see D3.

### F1. One marker slot per row, in both renderers

Priority `Rejected` (red) > `Unverified` (amber, unchanged copy) > `Per call` (neutral, when
`recollectedPaths` contains it). One slot, so the 210px label cell never stacks three pills.

`SectionMatrix` gains the marker as a 6px corner dot at cell top-left, clear of the dispute
cluster on the right — closing E2-#2 for 70% of the form.

Both share a new `ProvenanceTooltipBody` (attempt + mode, judge verdict with its score, the
authoritative note, evidence) which `DisputeTooltipBody` composes — closing E2-#1. The
`confidenceHighlightClass` tint stops being gated on `showDispute` for the rejected case
(E2-#3).

### F2. Group closure

On the group title row in `Section.tsx`'s `Rows`, and on the matrix band label cell:

> **1 of 32 answered but not counted — a retry re-asks this whole panel.**

Tooltip names the offending leaves and their judge verdicts, tracing back to the rejected
sibling. Reports the outermost enclosing group (E1).

### F3. Section-header risk

Beside the existing resolve pill: `3 unverified · 1 rejected`, folded into that button's
accessible name so a bulk accept states what it is accepting (E2-#4).

### F4. Review summary strip

A second row under the modal's status bar — outside the scroll container, so it stays
reachable in a form that scrolls both ways — Form tab only:

- `verified_pct` and `completion_pct` side by side, `review_reason`, retries used of
  `max_retries`, and counts for disputes / unverified / below-floor.
- A **next unresolved dispute** jump, reusing `data-field-path` + `scrollIntoView`
  (the pattern `CreatePatientFormModal.tsx:27` already uses).

**Deliberately no form-wide accept-all.** With 148 disputes that is the exact gesture that
would bulk-accept judge-rejected answers.

This strip is what carries S3: a form reading `READY_FOR_REVIEW · 0% verified · no call
reference` is legible for the first time.

Known limitation, accepted: a reviewer who collapses a section can jump to a hidden row,
because section open state is local to `Section.tsx`. Not lifting it.

### F5. Call history

Per-attempt changed list grouped by section; a path that changed in more than one attempt
annotated *"also changed in attempt 1"* — where the rep name and reference number become
visible as per-call facts, answering the product owner's first complaint.

**Stays values-free** (D2). Copy says "changed", never "collected", because review §15.3
means a repeating `collected_per="call"` answer never reaches `changed_paths` at all.

The attempt card stops implying scope, since D1 makes `mode` mean "was this call narrowed".

---

## Design — backend

### B1. `call_mode` and lineage describe the call (review §14.2)

`queue_dispatcher.py`: set a flag in the focus block and derive `call_mode` from **was the
plan narrowed**, not from `retry_count`; write `CallLineage` whenever the form has a prior
call, independent of mode. Both consumers of `Call.mode` — `call_provenance.py:115` and
`calls.py:1098` — are display-only, so no gate changes meaning.

### B2. Wire the floor (E6)

Add `retry_floor: int | None = None` to `schedule_dispatch_pass` / `run_dispatch_pass` /
`_dispatch_pass` — the same shape as `recording` and `plan_service`, so `vera_core` stays free
of `get_settings()` and the inject-settings-at-the-app-layer pattern holds — and pass
`settings.post_call_review_floor` at all four call sites: `api/v1/patient_forms.py:1630`,
`pipeline_sweeper.py:275`, `worker_events.py:837`, `api/v1/calls.py:845`.

### B3. Correct the docstring (E6)

`queue_dispatcher.py:161`. Leaving the "best-effort prompt guidance only" text in place is how
the next person re-introduces the bug.

### B4. Export tells the truth (E7)

Pass `authoritative_calls` (via the existing `_v2_doc_for` / `_authoritative_call_ids` at
`patient_forms.py:684,701`) to both loaders in the export endpoint, and add one column to each
of the two heading rows in `build_workbook`.

### B5. The DTO exposes the decision (E4)

Five read-only fields on `PatientFormDetail`: `verified_pct`, `review_reason`, `retry_count`,
`max_retries`, `review_floor` (the effective `settings.post_call_review_floor`, non-PHI).

### B6. One retry gate, one number

`post_call.py` computes the verified fraction **on the spot** at call close and gates on it,
replacing `form.completion_pct < T*100`. Both paths then read one semantics.

`recompute_form_projection` is **not** changed to write `verified_pct`: it runs on every
answer write, and `satisfied_required_fraction` needs `load_field_status` +
`load_authoritative_call_ids` — two extra queries per answer, ~540 on a 270-answer call. The
stored `verified_pct` column stays a display value refreshed at post-call eval and at
dispute-resolve; the gate never reads it. No migration, no hot-path cost.

Behaviour change is confined to where the two numbers diverge (E3): at T=0.80, S1/S4/S5 decide
identically and **S3 flips from park to retry** — a call that proved nothing earns another,
bounded by `max_retries`.

### B7. A confirm leaf needs a call (review §4.1)

`unsatisfied_required_paths` stops treating an intake value as satisfying a `role: confirm`
leaf. Per E8 this reduces to `policy_number` alone — the primary key of the conversation, which
a rep routinely confirms and whose non-confirmation means the call fundamentally failed.

**Gate 2 is otherwise untouched.** It is deliberately *not* tightened to require an
authoritative call: that is where a real retry storm lives, since a form whose rep never gives
a reference number could then never reach review. S3 is carried by F4, not by a gate change.

### B8. Re-anchor the confidence bands (E5)

> Numbered with the backend items because it consumes B5, but it edits
> `vera-frontend/src/lib/ibv/disputes.ts` — so it is implemented in the **frontend** plan,
> after B5 has shipped `review_floor`.

`confidenceLevel` takes the floor from B5: below floor -> red *"below the retry floor"*;
at or above -> two steps (>= 95 green, else neutral). One real boundary plus one
essentially-certain band, replacing three arbitrary ones. `answeredNotCounted` and F4's
"below-floor" count both read the same number.

A deliberate, reversible change to visuals reviewers are used to. Churn: `disputes.test.ts`
pins all four bands and `confidenceLabel`'s strings.

---

## Decisions recorded

- **D1 — `mode` means "was this call narrowed", not "was a retry budgeted".** Chosen over
  relabelling the UI (review §14.2 left it to the author). Whether a machine or an operator
  triggered the retry is irrelevant to how it is labelled and linked.
- **D2 — the per-attempt view stays values-free.** Values already live one tab away; the
  timeline's job is *which* facts moved. Adding them doubles the PHI surface for no new
  decision. Never in a URL, log or browser store.
- **D3 — explain observed causes, never predict the ask set.** Mirroring `focus_paths` needs
  `_required_paths(include_defaulted=True)` + `_alternatives` + `is_call_confirmed` + the
  floor. Review §14.5 already names hand-mirroring as the branch's weakest seam, and E6 shows
  the floor is env-configurable so a hardcoded mirror is wrong the moment it is set. The
  reviewer's real question is retrospective; judge rejection answers it exactly.
- **D4 — the closure note fires on "answered but not counted", not "never confirmed".** An
  intake-supplied answer is unconfirmed by design — S1 measures 0% verified, i.e. all 34 leaves
  in the population are unconfirmed on a never-dialed form — so the broader rule is pure noise.
- **D5 — no separate `retry_completion_threshold` knob.** Considered and rejected: it makes the
  E3 inconsistency *configurable* rather than removing it, leaves an operator two dials whose
  applicability depends on which consumer closed the call (surfaced nowhere), and costs a
  migration. Its one honest argument — that `verified_pct` may be stale on the fallback path —
  is answered by B6 computing it fresh.
- **D6 — no form-wide accept-all** (F4).

## Rejected / deferred, with reasons

- **Ship a `focus_preview` ask set on the detail DTO.** The escape hatch if the predictive view
  is ever wanted. Not built (D3).
- **`recompute_form_projection` writing `verified_pct`.** Hot-path cost (B6).
- **The sweeper / `call.ended` emitter defect.** `pipeline_sweeper` takes any non-terminal call
  older than `call_stuck_grace_seconds` (300s) and closes it once the LiveKit room is gone on
  two consecutive ticks. The worker sets `delete_room_on_close=True` and emits `call.ended`
  last, so past five minutes the room is already gone and the only protection is one ~60s tick.
  A confirmed dev incident stamped a 17m41s call at 99.32% completion `CALL_FAILED`, and
  `call_lifecycle.fail_and_requeue` then requeued it — **a fully successful call redialled the
  payer.** Root cause is worker-side: `CallLifecycleEmitter._emit` (`agent_worker/main.py:291`)
  swallows every exception, so one transient Redis error loses the call's terminal state
  permanently. Higher user impact than anything in this spec; out of scope because the fix is
  bounding the worker's shutdown path with `asyncio.timeout` plus a durable emit, which needs
  its own branch and tracing. **Logged, not fixed.**
- **A DSL way to declare "the payer may legitimately refuse this."** E8 shows `default`
  currently doubles as that marker. It works, but it is implicit — a leaf author who omits a
  default on a payer-refusable field points the retry loop at unobtainable data. Worth its own
  issue.
- **Review §14.1's remaining asymmetry.** The ask set includes defaulted leaves (39) while every
  gate excludes them (34), so a retry asks questions no gate measures. Deliberate per
  `focus_paths`' docstring; F4 must not imply those questions are owed.
- **`review_reason` on the worklist vs the modal.** Both now, via B5.

## Testing

Review §11 recorded **eight** tests on this branch that passed with their feature removed. So:
**every new assertion gets a named mutation that must fail it, run and recorded in the plan's
progress ledger.** Specifically:

- The new `FieldRow` / `SectionMatrix` marker tests **set provenance per-path**. Case 7 shipped
  because `FieldRow.test.tsx` mocked `provenanceFor: () => undefined`, so deleting the entire
  pill block left 637/637 green.
- Any schema-title assertion uses a title that differs from the humanized fallback —
  `"Copay ($)"`, the fixture `CallHistoryTab.test.tsx` already established for exactly this.
  Case 8 shipped because `toContain("Total")` is also satisfied by the fallback breadcrumb.
- The closure count is **hardcoded 32** in the closure test, with a **separate guard test**
  asserting the compiled artifact still has 32 collectable leaves under
  `labs_xray_ultrasound`. A test that derives its own expectation from the code under test is
  the case-4/5 vacuity pattern; a schema change must fail the guard, not silently rewrite the
  expectation.
- **B2's test is behavioural, not a call-kwarg assertion**: a settings floor != 70, a form whose
  only gap sits between the two floors, then assert the staged focused plan's ask set. A test
  that checks `try_dispatch` received the kwarg passes with the feature reduced to plumbing.
- A drift guard pins `settings.post_call_review_floor == REVIEW_CONFIDENCE_FLOOR` as the
  default, so the two cannot diverge silently without a `config` -> `forms` import.
- B6 gets S3 and S4 from E3 as fixtures, asserting both paths now decide identically.
- B7 gets E8's coverage-flip fixture, asserting the spouse leaves stay out of every gate and
  `policy_number` enters `unsatisfied` until a call confirms it.
- B4 needs a form carrying a **non-authoritative attempt** — review §13 notes the seeder has
  never built one, so the fixture is hand-rolled.

## Verification

Frontend, all four, every time:

```
cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build
```

Backend: `just check` from `vera-backend/`. Integration tests via `just test <path>`, never bare
`uv run pytest`.

Then `/simplify` over the change and re-run both sets.

**Merge gate: one live retry.** B6 and B7 change which forms redial live payers, so `just check`
is not sufficient — this needs a §15-style live round: `just seed-retry-form` +
`just arm-retry-form`, Langfuse up via the `langfuse-adc` skill's command (never
`just langfuse-up`; and `just langfuse-down` is not profile-scoped), one focused retry, then
confirm the attempt timeline labels it `retry` with a lineage row (B1) and that the gate decided
from the verified fraction (B6). Stop Langfuse afterwards — leaving it running inflates
`just check` ~3.4x (review §9.1).
