# Role-scoped gating input — an intake value must not answer a question the call owes

**Date:** 2026-08-10
**Branch context:** `fix/prompt-compiler`
**Follows:** `2026-08-09-lossless-call-plan-completion-design.md` (same area, different mechanism)

## Summary

A task's compiled question list is silently narrowed at task entry by answers the call never
collected. The schema's `default` for an `ask`-role leaf is written into `field_answer` at
intake; the worker cannot distinguish it from an answer, decides the gate it feeds is false,
and deletes every question behind it from the prompt.

On `closing_admin` this deletes the enrollment provider question. On `disease_only`'s
`policy_basics` it deletes the renewal-date question. Both are reproduced deterministically
below, and both are closed by one change: **the answer set a gate may be evaluated against
before the call collects anything is derived from leaf `role`, not dumped from intake.**

This is not a new rule. `role` already governs every other consumer of the same intake map —
`known_information` filters to `context`, `on_file_values` filters to `confirm`. The gate
evaluator is the one consumer that ignores it.

---

## Evidence

### The compiler is correct; the worker narrows

Building `closing_admin`'s plan straight from the catalog emits **9** questions, including:

```
2. What is the provider name and phone number for enrollment?
   - Enrollment Provider Name
   - Enrollment Provider Phone
   - Ask only if "Enrollment Required" is "Yes".
```

The prompt observed on a live call has 8, renumbered 1–8, with that question absent.
Re-rendering the compiled tree with those two paths excluded reproduces the observed prompt
byte-for-byte.

### Where the excluded answer comes from

`sections.enrollment.enrollment_required` declares `default="N/A"`. `IbvProvider.beginCreate`
(`vera-frontend/src/components/ibv/IbvProvider.tsx:476-479`) seeds every leaf's `default` into
the create payload, so every form is born with:

```
sections.enrollment.enrollment_required | "N/A" | source=intake
```

13 of 14 forms in the dev database carry that row. The value is a schema default, not an
answer — but by call time it is a `field_answer` row indistinguishable from one.

### What the call then does

Answer timeline from a live call (paths only):

```
15:35:57  enrollment.enrollment_required        <- Q1 asked; rep answers, gate flips to true
15:36:07  enrollment.center_of_excellence       <- Q2 (the provider question is not in the list)
15:36:21  authorization_department.*
15:36:40  third_party_administrator.*  ->  15:37:20  pharmacy_benefit_manager.*
15:38:32  infertility_treatment.*.cycle_limit   <- gap sweep begins (jumps back a task)
15:38:48  enrollment.enrollment_provider_name   <- swept up ~3 min late, out of context
15:38:55  enrollment.enrollment_provider_phone
```

The values exist only because the end-of-call sweep recovered them, spending its budget on a
question that should have been asked second. Had the rep answered `enrollment_required` with
anything other than `"Yes"`, nothing would have been asked and nothing would have been owed.

### Why `9e17401b` did not catch this

That commit stopped `owed_now` reading `leaf.default`, recovering `telehealth_covered`. It does
not reach this bug for three independent reasons:

1. **It stopped reading the word; this bug reads a row.** Nothing in this failure path consults
   `leaf.default`. It reads `answers["...enrollment_required"] == "N/A"`.
2. **It fixed the owed side; the question is deleted on the asked side.** `_apply_gating →
   excluded_fields → _settled → drop_questions` was untouched.
3. **`owed_now` would not have caught it either** — it filters on `is_applicable(field.gates,
   answers, shared)`, which reads the same value.

`telehealth_covered` was recoverable because it carries a default but **gates nothing**.
`enrollment_required`'s default gates two other questions, which is a different mechanism in a
different function.

---

## Root cause

`CallPlan.prefilled` is `dict[str, Any]` — every role, flattened, no provenance. It has exactly
two consumers in the repo, and both use it as an answer snapshot:

```
plan_runtime.py:714   self._answers = dict(plan.prefilled)     # gate evaluation
observer.py:360       self._answers = dict(plan.prefilled)     # dedup, rule engine, derivation
```

By the time `_settled()` asks whether `enrollment_required` is decided, the only fact available
is the string `"N/A"`. Whether the call was ever supposed to ask it — which the schema states,
in `role` — was discarded upstream.

Every gate consumer inherits that blind spot, which is why repairing them one at a time does not
converge.

### Why same-task gates are the exposed surface

`_settled(path, task_index)` is `_is_answered(path) or (owner < task_index)`. The position half
settles any gate whose source is collected by an **earlier** task, regardless of the seed. Only
**same-task** gates depend on `_is_answered` — and same-task is the dominant shape in these
schemas:

```
infertility_tx_covered      -> gates 72 questions in its own task
diagnostic_testing_covered  -> gates 32 questions in its own task
male_partner_covered        -> gates  8 questions in its own task
enrollment_required         -> gates  2 questions in its own task
```

One prefilled gate field deletes everything behind it. Today that costs two questions.

---

## Design

### One rule

> An `ask`-role leaf is collected **on the call**. A value on file for one before the call is a
> pre-call baseline, never an answer, and must not settle a gate.
>
> `confirm` stays authoritative — it is on file to be read back (the member-ID pattern).
> `context` / `input` stay authoritative — they are what the clinic supplied.

### The two maps are different concepts and get different names

They are not interchangeable, and calling both `_answers` is what let this slip past the last
fix. Only the controller's changes.

**`PlanRunController`** — what a gate may be evaluated against:

```python
# vera_core/forms/call_plan.py
def gating_seed(plan: CallPlan) -> dict[str, Any]:
    """Answers a gate may be judged against before the call has collected anything.

    An `ask`-role leaf is collected ON the call, so an intake value for one is a pre-call
    baseline, not an answer, and must not settle a gate. `confirm` stays (on file, to be
    read back); paths that are not collectable at all are clinic-supplied context."""
    asked = {f.path for t in plan.tasks for f in t.fields if f.role == "ask"}
    return {path: value for path, value in plan.prefilled.items() if path not in asked}
```

`role` is already on `PlanFieldDescriptor` (`Literal["ask", "confirm"]`), so **no `CallPlan`
schema change** — which matters, because the plan is `extra="forbid"` and persists in Redis, so
a new field would be a rolling-deploy hazard.

**`ObserverManager`** keeps `dict(plan.prefilled)`, renamed `_on_file` to say what it is. Three
documented behaviors require it to hold intake values, and all three are load-bearing:

| Site | Documented dependency |
|---|---|
| `observer.py:503` dedup | *"INTENTIONALLY covers the intake prefill seed too: a rep merely confirming a prefilled value leaves no ai_call row"* |
| `_derive_remaining_locked` | *"a rep-stated or prefilled remaining wins — never overwrite it"* |
| `rule_engine.evaluate` | `no_out_of_network_coverage` is a **terminate** rule over three `ask`-role paths that carry human-typed intake rows in the dev DB |

Changing the Observer's seed could change when a call ends. It is not needed for this bug and is
explicitly out of scope.

### Behavior after the change

At `closing_admin` entry, `enrollment_required` is unanswered, so its gate is undecided and the
provider question stays in the list carrying its own prose gate. The agent asks Q1; the rep
answers; the agent evaluates the stated condition and asks Q2 or skips it. The Observer's write
then settles the gate for the sweep. No re-render is required, and the mechanism is identical to
how PBM and infertility-specialty-pharmacy already behave in the same task.

---

## What this does not change

| Concern | Why it is unaffected |
|---|---|
| `completion_pct` / auto-retry threshold | Computed control-plane-side from `field_answer` rows (`review.completion_pct_v2`), never from the worker's map. The intake rows still exist and still count |
| Export | `export_form_sheet.py:239` applies the §4.4 default fallback from the leaf, independent of the worker |
| `known_information`, `on_file_values`, `{{token}}` hydration | All read `values` directly inside `PrefillFuser.fuse`; none touches `prefilled` |
| Compiler↔worker decisiveness invariant | `question_plan._entry_decided` requires the worker be at least as decisive as the compiler. Only the `_is_answered` half of `_settled` shrinks; the position half **is** the compiler's rule and is untouched. For same-task gates the worker becomes exactly as decisive as the compiler instead of more so |
| Task-level `applicable_when` | Neither catalog defines one, so `_next_applicable` / `_next_gap_task` never evaluate one |
| `is_satisfied` | Not in the guard path since `9e17401b`. Its only production caller is `completion_pct_v2` |

---

## Measured effect

Real compiled plan, real `PlanRunController`, intake values as they appear in the dev DB.

**`ibv_standard` / `closing_admin`:**

```
TODAY   excluded: enrollment_provider_name, enrollment_provider_phone, auth_department_*
        questions in list: 7    gap_fields: center_of_excellence, tpa, pbm, isp

FIXED   excluded: auth_department_*
        questions in list: 8    gap_fields: enrollment_required, center_of_excellence, tpa, pbm, isp
```

**`disease_only` / `policy_basics`:**

```
TODAY   excluded: renewal_date     questions in list: 10
FIXED   excluded: (none)           questions in list: 11
```

One change closes both halves of the failure: the question returns to the prompt, **and**
`enrollment_required` becomes genuinely owed to the completion guard (it was invisible before
because `owed_now` requires `not has_value`, and `"N/A"` is a value).

### Regression surface, measured

`coverage_type` is the largest human-filled `ask` field — 7 of 14 forms, gating 17 questions —
and is **unaffected**, because all 17 live in later tasks and the position rule decides them
either way:

```
male_partner under Individual coverage
  TODAY  applicable=0 excluded=9 conditional=0
  FIXED  applicable=0 excluded=9 conditional=0
```

Across both catalogs, the full set of behavior changes on real data:

| Path | Forms | Effect |
|---|---|---|
| `enrollment.enrollment_required` | 13 | 2 questions restored — the reported bug |
| `coverage_summary.benefit_year_type` (disease_only) | — | 1 question restored |
| `insurance_information.plan_type` | 3 | 1 question moves *excluded* → *conditional* |
| `insurance_information.doctor_inside_network` | 1 | 1 question moves *excluded* → *conditional* |
| `insurance_information.facility_inside_network` | 1 | 1 question moves *excluded* → *conditional* |
| `telehealth_covered`, `policy_situs`, `group_name`, `group_number`, `pcp_referral_required` | 14 | none — they gate nothing |

*Conditional* means the question stays listed with its prose gate for the agent to evaluate
live — the normal path for any gate that is not decidable at entry.

A prototype of the seed change passes the worker + forms unit suites unchanged
(`777 passed, 1 xfailed`). **That is also the problem:** no existing test pins the current
behavior, so nothing would catch a re-break. Tests are part of this change, not a follow-up.

---

## The residual trap, and the rule that closes it

The seed fix removes `ask` from the gating input, so a `default` on an `ask`-role leaf becomes
harmless for gating. `confirm` values, by design, stay authoritative — which means **a `confirm`
leaf carrying a `default` would still silently delete every question gated on it**, by exactly
the mechanism this spec removes for `ask`.

There are zero such leaves in either catalog today. The rule is therefore a trap-closer, not a
fix, and it requires no catalog change:

> **Validator rule.** A `confirm`-role leaf that declares a `default` may not be referenced by
> another leaf's gate chain.

It belongs in `compile_document` rather than `_validate_document` — `_validate_document` runs on
every load, including the dispatcher's per-call path, where `validate_question_coverage` already
costs ~1–2 s of control-plane CPU per call.

---

## Testing

1. **Regression, `ibv_standard`** — with `enrollment_required` prefilled `"N/A"`, neither
   provider path appears in `excluded_fields(closing_admin)`, and the rendered list carries the
   provider question. Assert on the paths, not on a bare count: the task's total also depends on
   whether `any_service_requires_prior_auth` has resolved, which is what makes the count 8 in the
   measurement above and 9 on a call where prior-auth answers exist.
2. **Regression, `disease_only`** — with `benefit_year_type` prefilled `"Calendar Year"`,
   `policy_basics` renders 11 and excludes nothing.
3. **`confirm` stays authoritative** — a prefilled `confirm` leaf still settles its gate, and
   `on_file_values` still lists it for read-back.
4. **`context` stays authoritative** — `spouse_gender` prefilled `"N/A"` still excludes all 9
   male-partner questions.
5. **Position rule intact** — `coverage_type` prefilled `"Individual"` still excludes the 9
   `male_partner` fields (later task), proving the change does not weaken cross-task decisions.
6. **Owed set** — `enrollment_required` appears in `gap_fields` and `owed_question_count` until
   the call answers it.
7. **Observer untouched** — its map still seeds from `prefilled`; the dedup behavior for a rep
   confirming a prefilled value is unchanged.
8. **Validator** — a synthetic doc with a defaulted `confirm` leaf referenced by a gate fails
   `compile_document`; both real catalogs compile clean.

A change to spoken output is not verified by `pytest` (repo `CLAUDE.md`). Sign-off needs a live
call on browser-callee transport, confirming `closing_admin` speaks all 9 questions in order and
the enrollment provider question is asked in position 2 rather than swept at the end.

---

## Out of scope

- **Frontend default seeding.** With the seed filtered, those rows are inert for gating.
  Removing them is data honesty, not correctness, and it drags in a decision about existing
  rows. Separate change.
- **`is_satisfied`'s `default` clause.** Its only caller is `completion_pct_v2`, where a
  defaulted `ask` leaf counts as filled and can suppress an auto-retry. Real, but a different
  blast radius (the retry path) and unrelated to the reported bug. Its docstring is stale — it
  still claims `gap_fields` uses it — and that one-line correction is included here.
- **The Observer's seed**, for the reasons tabled above.
- **`infertility_specialty_pharmacy` collecting no answers** on the observed call. Noticed while
  reading the timeline; not investigated.
