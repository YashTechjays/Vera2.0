# Plan — alternatives-aware completion, and filling the unused side of an either/or

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answering one side of an either/or must satisfy the pair everywhere — the gap sweep, both
completion guards, the form's completion %, the export's `*` marker — and the unused side must carry
its declared value so the **export**, which is the platform's final product, reads `$0` / `0%` / `N/A`
rather than blank. The unchosen branch of a routing alternatives must stop being owed too.

**Tech Stack:** Python 3.12, pydantic v2, pytest; TypeScript for the frontend mirror.

---

## Evidence

Live call, Langfuse trace `98df80927c4e3a5c83fed68286901921`. The `infertility_coverage` gap sweep
owed **13** fields; **ten were phantom** — for each, only one side of the pair was owed, so the other
had been answered:

```
Copay ($) (ovulation_induction)   Coinsurance (%) (cpt_58970)   Copay ($) (cpt_89258)
Copay ($) (cpt_58323)             Coinsurance (%) (cpt_89280)   Copay ($) (cpt_89342)
Copay ($) (cpt_58322)             Coinsurance (%) (cpt_89253)   Copay ($) (cpt_89337)
Copay ($) (cpt_89261)
```

The bot knew: its `gap_complete` reason was *"the representative confirmed ovulation induction is
covered under coinsurance rather than a copay, so no copay applies."* It was refused anyway and
burned refusals on gaps that can never close. The other 3 (`Covered` for cpt_58974, cpt_89290,
cpt_89291) are genuine — those panels were never asked, tracked with the premature-`task_complete`
work, not here.

## Root cause

`cost_pair` (`authoring.py:307`) declares the either/or in its docstring. But `Section.alternatives`
is read **only** by `question_plan.py`, to build one spoken question. Nothing in the completion path
consults it: `is_required` (`conditions.py:102`) resolves only `bool | RequiredWhen`, and
`PlanFieldDescriptor` carries no grouping, so the worker is structurally blind.

Two further defects found while scoping, both in scope here:

- **Routing branches are never gated on the choice.** `asc_professional`/`asc_facility` have
  `applicable_when=None`; both egg-cryo branches are gated only on `infertility_covered`. The choice
  is enforced in prose ("take only the matching panel below"), so the unchosen branch's `covered`
  stays owed for the rest of the call. Exactly **one** field per branch — its cost-sharing children
  are already gated behind `covered == "Yes"`.
- **`PlanFieldDescriptor` does not carry `default`.** `completion_pct_v2` (`review.py:155`) counts a
  leaf with a `default` as filled, and the export writes it (`export_form_sheet.py:239`) — but
  `gap_fields` cannot see it. Six `ibv_standard` leaves are affected (`group_name`, `group_number`,
  `policy_situs`, `pcp_referral_required`, `telehealth_covered`, `enrollment_required`, all
  `required=True, default='N/A'`): **counted as filled by the form, still chased by the bot.** This
  violates the invariant `gap_fields`' own docstring asserts.

## Decisions

1. **Satisfaction, not applicability.** A set is satisfied when a member has a value; nothing is made
   inapplicable. Gating the unused side (`copay.applicable_when = not <coinsurance answered>`) was
   rejected: when both sides legitimately have values — which is *not* uncommon — both gates go
   false, both fields become inapplicable, and the export writes nothing for an inapplicable cell
   (`export_form_sheet.py:247`), so two real answers would vanish from the deliverable.
2. **Satisfaction is per PARENT PATH, not per authored set.** `panel_cost_pairs`
   (`authoring.py:244`) flattens every code's copay *and* coinsurance into one `Alternatives` —
   the diagnostic panel is **16 members across 8 CPT codes**. Treating the whole set as satisfied by
   any one member would mark 16 fields done off one answer and fill the other 15. Members are grouped
   by `path.rsplit(".", 1)[0]`, recovering the `cost_pair(base) -> [base.copay, base.coinsurance]`
   pairs the flattening erased.
   *Identical to set-level behaviour when the Observer fans out (verified on the trace: within every
   panel, either all codes' copay or all codes' coinsurance was answered, never mixed), and strictly
   safer when it doesn't — a visible gap beats a wrong number in the final product.*
3. **No `is_answered` operator.** Considered and dropped: with satisfaction the rule is plain code
   over the alternatives set, so a new `ComparisonOp` would have no caller. (`ne ""` already works
   anyway — `_as_text(None)` returns `""` and `conditions.ts:6` documents the same.)
4. **`default` is the wrong mechanism for this.** It is unconditional — "complete without asking,
   always". `default='$0'` on copay would count it filled even when the rep gave *neither* side,
   masking the genuine gaps this plan exists to expose. Keep `default` for its current job; fix only
   that the worker cannot see it.
5. **The fill is written, `source=ai_call`** (owner's call, 2026-08-07). The export is the platform's
   final product and must read `$0` / `N/A`; a placeholder would leave the cell blank
   (`export_form_sheet.py:239` writes only a `default`). Accepted tradeoff, recorded because it is
   not self-evident: a reviewer cannot distinguish a derived `Copay: $0` from a spoken one. The
   alternative was `AnswerSource.DERIVED` + a migration recreating the `source` CHECK constraint.
6. **Fill values come from the leaf's existing `inapplicable_value`.** Audited legal: `$0` is in
   copay's `special_values`; `0%` suits a percent range; `N/A` is in every `covered` enum's `values`.
   All 14 authored cost-pair sets declare one on both members — nothing invented.
7. **Never fill a value that SATISFIES a downstream gate.** Filling `N/A`/`No` closes dependents,
   which is correct and desirable — filling `asc_professional.cpt_58555.covered = "N/A"` makes its
   copay/coinsurance/prior_auth inapplicable in one step. Filling `"Yes"` would *create* required
   questions: asserting `cpt_89342.covered = "Yes"` would conjure
   `embryo_cryo_storage.storage_time_coverage` out of nothing. Mechanically checkable, so enforce it.
8. **Emptiness is checked against the form's CURRENT value, not the call span.** `FieldAnswer` holds
   one current value per `(form_id, field_path)` and `call_id` is nullable. A call-scoped check would
   let a focused retry (new `call_id`) fill `$0` over a real `$25` from call 1, or over a human edit
   (`call_id = NULL`).

## Global Constraints

- `gap_fields`' set and `completion_pct_v2`'s set must stay identical — `gap_fields`' docstring says
  it counts *"the same required/applicable set the form's completion percentage counts"*.
- The frontend mirrors this: `completion_pct_v2`'s docstring and `forms/CLAUDE.md` require keeping
  `vera-frontend/src/lib/ibv/conditions.ts` in sync.
- A fill must never overwrite an existing current value; a later real answer must overwrite a fill.
- Never log a field value.
- No grammar change, so no `dsl_version` bump.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/call_plan.py` — carry the alternatives pairs
  (grouped per Decision 2) and each leaf's `default` onto the plan
- **Modify** `packages/vera_core/src/vera_core/forms/conditions.py` — the shared satisfaction
  predicate, beside `is_required`/`is_applicable`
- **Modify** `apps/agent_worker/src/agent_worker/plan_runtime.py` — `gap_fields`, and honour `default`
- **Modify** `packages/vera_core/src/vera_core/forms/review.py` — `completion_pct_v2`, `:247`
- **Modify** `packages/vera_core/src/vera_core/forms/export_form_sheet.py` — the `*` marker (`:265`)
- **Modify** the Observer / answer-merge path — the fill
- **Modify** `vera-frontend/src/lib/ibv/conditions.ts` (+ `conditions.test.ts`)
- **Tests:** `tests/unit/forms/test_conditions.py`, `test_review.py`, `test_call_plan.py`,
  `apps/agent_worker/tests/unit/test_plan_runtime.py`

---

### Task 1: carry the pairs and the defaults into the compiled plan

`CallPlan` gains the alternatives pairs — leaf members only, grouped by parent path per Decision 2;
group-level sets are routing and handled in Task 5. `PlanFieldDescriptor` gains `default`.

Tests: the diagnostic 16-member set compiles to **8** pairs, one per CPT code, not one set of 16; the
two group-level sets are excluded; the six default-bearing leaves carry their `default`.

### Task 2: one shared satisfaction predicate, used by every consumer

A field is **not owed** when another member of its pair has a value, or when it declares a `default`.
Put it in `conditions.py` so both services import one rule, then call it from `gap_fields`,
`completion_pct_v2`, `review.py:247` and the export marker, and mirror it in `conditions.ts`.

Tests: coinsurance answered → copay not owed, not counted, unstarred; **both answered → neither owed
and BOTH still displayed** (Decision 1's regression); neither answered → both owed and
`owed_question_count` collapses them to one ask; one code's copay answered does **not** satisfy a
different code in the same authored set (Decision 2's regression); a default-bearing leaf is not
owed.

### Task 3: fill the unused side

When a pair becomes satisfied, write each empty member's `inapplicable_value` with `source=ai_call`.
Guards: only when there is no current value for `(form_id, field_path)` (Decision 8); never a value
that satisfies a downstream gate (Decision 7); skip a member with no `inapplicable_value`; idempotent.

Tests: coinsurance answered → copay written `$0` and the export cell reads `$0`; a real copay arriving
later replaces the fill; a second pass writes nothing; both-answered writes nothing; a member without
an `inapplicable_value` stays blank but is not owed.

### Task 4: verify the export reads what the client should see

The point of Task 3. Assert the generated sheet shows `$0` / `0%` / `N/A` for filled members and the
real value where the rep answered — not blank, and not grey.

### Task 5: the unchosen routing branch

When any leaf under a sibling branch has a value and **this branch has none at all**, fill this
branch's `covered` with `N/A`. Its cost-sharing children are already gated on `covered == "Yes"`, so
one write makes the branch inapplicable — no gate authoring and no new `inapplicable_value` needed.
The "this branch has none at all" condition is what keeps a legitimately-both-answered case intact.

Tests: ASC facility answered → `asc_professional.cpt_58555.covered` filled `N/A` and its cost-sharing
inapplicable; both branches answered → nothing filled; egg-cryo elective/cancer likewise.

### Task 6: verify

`just check`; `/simplify`; `just check`. Frontend: `tsc` + `eslint` + tests + build. Then the eval
harness, then a live call: `infertility_coverage`'s sweep should owe **3**, not 13, and those 3 should
be the genuine `Covered` gaps. Inspect the exported sheet for the filled cells.

---

## Out of scope

- The premature-`task_complete` residue: the same call asked ~31 of 41 `infertility_coverage`
  questions. Separate defect; do not conflate.
- **Fix 2 (the refusal-guard once-per-task latch) must not land before this.** Removing the latch
  while phantom gaps exist would make the guard refuse `task_complete` repeatedly for fields that can
  never be filled. The latch is currently the only thing bounding that.
- `_owning_segment` renders both egg-cryo branches as `cpt_89337`, so the gap list cannot tell them
  apart. Real, separate, worth fixing.
- `storage_time_coverage` has no `inapplicable_value`, so its placeholder is blank when
  `cpt_89342.covered != "Yes"`. Display-only nit; it is already correctly excluded from completion.
