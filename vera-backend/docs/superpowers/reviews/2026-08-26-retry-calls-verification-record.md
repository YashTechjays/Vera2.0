# `fix/retry-calls` — verification record

**Date:** 2026-08-26 · **Branch head at writing:** `05087823` · 76 commits ahead of `origin/dev`

Why this file exists: the SDD ledger that carried the live-call evidence, the mutation proofs
and the decisions taken during this branch lives under `.superpowers/`, which is **git-ignored**.
None of it would survive the merge, and a PR reviewer would have no way to see that the live
gate happened at all. This is the durable copy.

---

## 1. What the branch contains

Three pieces of work, in dependency order:

1. **The retry-decision backend** (spec B1–B7, merged earlier) — `focus_paths`, authoritative
   calls, `CallMode.RETRY` + lineage, `verified_pct`.
2. **The Observer unchanged-skip provenance fix** —
   `docs/superpowers/specs/2026-08-26-unchanged-skip-discards-provenance-design.md`.
3. **Two fixes found while verifying (2)** — the per-call dispute-exemption marking in the
   review UI, and the park-vs-redial decision extraction described in §4.

---

## 2. LIVE GATE — taken 2026-08-26, PASSED

Form `01a03d0e-cd72-7032-8537-fafd95765110`, four real calls over browser-callee transport.

| | call | mode | outcome |
| --- | --- | --- | --- |
| 1 | `01a03d0f-459c-71a0-953a-a14f92d66bfb` | full | 152 rows, **no reference captured** → non-authoritative |
| 2 | `01a03d24-c701-7db2-97c7-9872e328363e` | full | 137 rows, reference `abc7938` → **authoritative** |
| 3 | `01a03d5a-06e5-73c2-a608-8050e73ad20e` | **retry** | focused: 3 leaves asked against 166 inherited values |
| 4 | `01a03d76-1370-7930-9b7d-0134b0e2b147` | **retry** | rep said "not active" → `terminated_by_flow_rule = True` |

### The proof (Scenario A)

Six fields the rep repeated **verbatim** were each written by call 2 as a single clean row whose
value is byte-identical to what call 1 held. Pre-fix every one hits the skip and call 1 keeps the
row forever:

```
coverage_type          'Individual'     call 1 -> call 2
plan_fund_type         'Self Insured'   call 1 -> call 2
benefit_year_type      'Calendar Year'  call 1 -> call 2
employer_support_size  'Large Group'    call 1 -> call 2
policy_number          'KJE23423'       call 1 -> call 2   <- role=confirm, required=True
is_insurance_active    'Yes'            call 1 -> call 2   <- collected_per="call"
```

`group_name` and `rep_name` also moved but their values differed trivially (`alpha`/`Alpha`,
`Jack S.`/`Jack S`), so they would have been written pre-fix too and are **not** part of the proof.

Call 1's current rows collapsed **140 → 24**; call 2 owns 124. Form reached
`completion_pct 100.00`, `verified_pct NULL → 100.00`, `review_reason ready_for_review`, audit
`trigger=post_call_eval` with 124 `field_evaluation` rows.

`policy_number` being in that set closes a **second defect** not in the original brief: it is
`role="confirm", required=True`, and `_satisfied` refuses to count an intake value for a confirm
leaf (`review.py:356`). Pre-fix, a rep confirming the read-back member ID wrote no row, so the
field whose entire purpose is payer confirmation could only ever be satisfied by the rep
*contradicting* it.

### Scenario B — focused retry

Calls 3 and 4 both dispatched focused (`has_call_reference` true): `focus_paths` returned exactly
3 leaves while `_on_file` still carried 166 inherited values, because `focus_call_plan` clears
`on_file_values` but not `prefilled`. Call 3 proved review §15.3 — `is_insurance_active` was
already `'Yes'` from call 2, the rep said `'Yes'` again, and the row is now owned by call 3.

### Risks checked and NOT observed

- **Spec §3.5 (judge rejects a terse confirmation).** All six confirmed prefills came back
  `supported=True, confidence=100`; across all 124 of call 2's answers, **zero unsupported, zero
  below floor**. One call is not proof it never happens, but the accepted trade-off looks benign.
- **Spec §2.3 side effect, observed as predicted.** Call 2 wrote derived `remaining` values
  (`$28,000`, `$7,000`) from a confirmation-only pass — the early return used to short-circuit
  before `_derive_remaining_locked`. No unexpected terminate or task skip fired.

### Not reachable, and now a structural finding rather than an untested gap

B2 — a *confirmation* firing a rule off inherited state. Call 4's `is_insurance_active` went
`'Yes' → 'No'`, a **changed** value, so that write and that rule firing are pre-fix behaviour. And
the focused plan had nothing to skip (task 0 and task 8 only, so `skip_to_task: wrap_up` and
carrying on are the same trajectory — 1m45s vs call 3's 1m44s). The harmful shape is impossible
on this schema: `is_insurance_active` is the **only** collectable leaf in task 0, which runs
first, so the bot always asks the question before any write can fire the rule.

---

## 3. Mutation evidence

Every new assertion was proved capable of failing. Each mutation was reproduced by an independent
reviewer, not read from the implementer's report.

| mutation | result |
| --- | --- |
| `observer.py` seed → `dict(plan.prefilled)` (drop `canonical_answer`) | `assert [] == [Terminate(rule_key='stop')]` |
| restore the `_on_file` dedup key | 4 tests red |
| delete the dedup guard entirely | `assert 3 == 1`, `assert 2 == 1` |
| widen `field_answers.py:111` to `if current is not None:` | both supersede tests red |
| invert the early return at `review.py:105` | both dispute tests red |
| `review.py:266` → `ai_supported is not False` | the unjudged assertion alone red |
| delete the post-write `_on_file[path] = value` | 7 red across rule-engine + derivation |
| `fieldUsageOf` ignores the call-scoped set | 2 red |
| per-call tint back to `bg-amber-100` | palette-collision test red |
| `UsageLegend` ignores the set (the bug shipped in `379748ba`) | 2 red |
| `decide_retry`: drop the askability guard | `Redial()` where `UNSATISFIED_UNASKABLE` expected |

---

## 4. The park-vs-redial extraction, and the bug it uncovered

The two post-call resolvers shared the retry **number** but not the **decision**: `evaluate_call`
applies "nothing is unsatisfied" and "nothing unsatisfied is askable"; `resolve_ai_processing`
applied neither. A form whose only remaining gaps were unaskable parked on one path and redialed
on the other.

Investigating that turned up something worse. `is_call_confirmed` requires a judge verdict
(`ai_supported`), and the fallback runs precisely when no judge ran. `bool(None)` is `False`, so
`satisfied_required_fraction` returns **exactly 0.0 there for every call however good** — and 0.0
is below every threshold. **With auto-retry on, the fallback redialed every form until
`max_retries`, against real payers.** The two guards would not have fixed it: without a judge,
`is_field_satisfied` also fails, so `unsatisfied` and `retryable` are both non-empty and the rule
still says redial.

This also re-explains form `01a039e6` burning all 5 retries earlier in this work — that run had
`post_call_eval_ready` forced `False`, so it was on this path. It was previously attributed solely
to the unchanged-skip defect.

**Fix:** `services/retry_decision.decide_retry` — pure, no session or ORM, **one caller**
(`evaluate_call`). `resolve_ai_processing` makes no fill-based retry decision at all; it does its
real job, guaranteeing the form leaves `AI_PROCESSING` with an honest reason so a crash between
closeout and resolution cannot strand it. Legacy v1's `completion_pct` branch removed (no v1
support needed pre-production). `auto_retry_enabled`, `review_floor` and the bool return are dead
on that resolver and were removed rather than left to mislead.

**Behaviour note for whoever turns auto-retry on:** with the deployment switch off this change is
nearly invisible — the fallback parked before and parks now. It only bites when auto-retry is
enabled, so QA enabling it is the first real exercise of the new path.

---

## 5. Gates

| gate | result |
| --- | --- |
| `just check` | **2726 passed**, 3 skipped, 21 deselected, 1 xfailed — **0 failed, 0 errors** |
| `mypy --strict` | clean, 399 source files |
| `ruff check` + `ruff format --check` | clean |
| frontend `tsc -b` / `eslint` / `npm test` / `npm run build` | clean / clean / **674 passed** (90 files) / clean |
| boot check (control plane) | "Application startup complete", **0** error/traceback/back-off lines over 150s = 2.5 sweeper windows |
| live gate | **PASSED** — §2 |

The boot check was run because this branch removes parameters from `resolve_ai_processing`, which
the pipeline sweeper and the worker-event consumer both call from long-lived loops.

Two `just check` observations worth knowing: a run mid-session showed **654 skipped** and a direct
integration run showed 4 failures, both of which vanished on re-run — the shared-dev-DB contention
signature. Re-run before debugging that pattern.

---

## 6. Decisions taken during the work

| # | decision | cost if wrong |
| --- | --- | --- |
| R1 | Mutation evidence duplicated into the branch ledger, not only the SDD workspace | evidence in two files |
| R2 | Skipped the plan's first dispute mutation — it cannot discriminate, so it would have produced a false "mutation-proved" claim | one wasted attempt |
| R3 | Live gate handed to a human; tasks 1–6 executed automatically | none |
| R4 | Tasks 3/4/5 batched into one dispatch, one commit each | a defect in one blocks the batch's review |
| R5 | The final fix wave added a test beyond the three spec addenda asked for, because the discovered behaviour change had zero coverage | one extra test |
| R6 | Skipped the simplify pass on a 16-line test copying a sibling verbatim | an unreviewed test three reviewers read |
| R7 | Merged `origin/dev` rather than rebasing — rebase rewrites all SHAs and the ledger anchors mutation evidence to them | a merge commit in the history |
| R8 | `UsageLegend` reads the call-scoped set from the provider instead of taking a prop — a prop left the identical bug one call site away | slight provider coupling in a presentational component |

### Defects in my own planning, caught by others

- The plan asserted a test passes before the production change; it does not (the old guard skips
  every turn when the prefill equals the extracted value, so it failed `assert 0 == 1`). The
  dispatch note entrenched the error. Caught by the implementer.
- A plan code snippet did not survive `mypy --strict` (`session.current.is_current` is union-attr).
  Caught by the implementer.
- Spec §2.3 claimed `_on_file`'s other jobs were unchanged — true of the map's contents, false of
  the *invocation*. Caught by the final review; it changed what the live gate had to watch for.
- The per-call tint shipped with no legend row and in a colour that already meant "low-confidence
  dispute". Caught by the user, from a screenshot.
- The live-gate procedure omitted `VERA_GCP_PROJECT`, so call 1 ran with no judge. Caught by the
  void-check before call 2, which is why the gate is still valid.

---

## 7. Outstanding — not fixed, tracked

Follow-ups **F-a** … **F-f** are in
`docs/superpowers/specs/2026-08-26-unchanged-skip-discards-provenance-design.md` §7.
The **F0–F5** frontend plan is in `docs/superpowers/specs/2026-08-25-per-call-answers-review-ux-design.md`.

Highest value first:

1. **F0–F5 frontend work**, absorbing **F-b** (realtime Unverified clearing — the pill does not
   clear during a call, by design: the `field_answer` SSE envelope carries no provenance and
   `LiveCallModal` does not refetch mid-call) and **F-a** (the `source` display flips
   `intake → ai_call` for a confirmed prefill). Also the per-attempt snapshot viewer: the data
   already exists in `call_form_snapshot.before_state` / `after_state`, and the Call history tab
   currently shows changed-path labels with no values.
2. **F-d** — pre-2026-08-15 forms carry non-canonical intake rows this fix will re-record and
   dispute; disputes gate the human `→ COMPLETED` transition. Worth a data check before rollout.
3. **F-f** — extractor spelling instability on free-text leaves (`alpha`/`Alpha`,
   `5 cycles per year`/`5 cycle per year`). Seen on three of four gate calls. Churn and judge
   fan-out, not correctness. **Explicitly deferred by the product owner.**
4. **F-c / F-e** — documented caveats, no action.

One observation for a reviewer's eye, not a defect: after call 4 the form reads
`verified_pct 100.00, ready_for_review` with `is_insurance_active = 'No'`. Honest by the
definition — everything asked *was* confirmed by an authoritative call — but it reads oddly.
