# `fix/retry-calls` — verification record

**Date:** 2026-08-26 · **Branch head at writing:** `05087823` · 76 commits ahead of `origin/dev`

**Review round:** 2026-08-27 · head `9da02279` · 87 commits ahead. Nine commits from a PR
review, one of which replaced the retry SCOPE GATE — so §2's gate is no longer the shipping
one. §8 carries that round's own live call, mutation evidence and gates.

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

> The FOCUSED-vs-FRESH gate exercised here (`has_call_reference`) was deleted in the review
> round; see §8. Nothing below is retracted — these four calls remain the evidence for the
> unchanged-skip fix, and their outcomes hold under the new gate: the reference leaf on this
> form carries three rows, all `source=ai_call` (calls 2, 3 and 4) and none hand-edited, so the
> authoritative set is non-empty at every dispatch and calls 3 and 4 still run FOCUSED. But the
> gate they ran through is not the one that ships.

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

These are the numbers for head `05087823`. The review round's gates are in §8.

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

---

## 8. Review round — 2026-08-27, head `9da02279`

Nine code commits (`b2f52a1b`..`9da02279`) answering a PR review, plus this record.

All six confirmed findings fixed — one of them (§8.5) answered differently from how it was
asked, with the reasoning recorded. One reported finding rejected as already-decided (§8.5).
Two further defects fixed that were not on the list: the `provenance` clear on modal close
(`cb811266`, its own test and own commit) and the retry seeder the gate change broke (§8.6).

### 8.1 The finding that mattered — the scope gate read the wrong row set

`has_call_reference` read `load_field_status`, which filters `is_current`.
`load_authoritative_call_ids` deliberately does not. `resolve_disputes`' human-write loop is
`for path, new_value in body.form_data.items()` with no exclusion for call-scoped paths, so a
reviewer editing **Call Reference Number** writes `source=human, call_id=None` and supersedes
the call's row. The two gates then disagreed exactly there: the next dispatch skipped the focus
block entirely and dialled a FULL call, re-asking ~150 fields that were all still
call-confirmed, with `verified_pct` high throughout.

Fixed by reading the gate off the same row set `focus_paths` already uses. `has_call_reference`
is **deleted**, not repointed: its whole contract was "the CURRENT answer at the reference
path", which is the defect, and once it reads the authoritative set it is `bool(set)`. The D8
intent survives — a human-typed reference has no `call_id`, so a form that was never dialled
still runs FRESH.

The narrower of two directions. The other — excluding `collected_per="call"` paths from the
human-write loop — may be independently correct, but it changes what a reviewer is allowed to
edit and this bug does not require it. Left open.

### 8.2 LIVE GATE — taken 2026-08-27, PASSED

Form `01a040e4-1823-74f3-9a8a-73108dcbc458`, two real calls, with a hand edit between them.

| | when | call | mode | outcome |
| --- | --- | --- | --- | --- |
| 1 | 01:44:45 | `01a040e4-1827-7932-9984-22b8f14dc168` | full | 152 answers over 16 sections, reference captured |
| — | 01:46:55 | — | — | **reference number edited by hand** → `source=human, call_id=None, is_current` |
| 2 | 01:47:06 | `01a040e6-40ab-7242-8608-46da64db0506` | **retry** | 44 answers over 6 sections |
| — | 01:51:57 | — | — | call 2 captured its OWN reference (`ai_call`, `call_id=…e6`) |

Every row at the reference-number leaf, which is the whole proof:

```
01:44:45  source=ai_call  call_id=01a040e4  is_current=False
01:46:55  source=human    call_id=None      is_current=False   <- current at dispatch
01:51:57  source=ai_call  call_id=01a040e6  is_current=True
```

**The counterfactual is exact.** At 01:47:06 the current row was the human edit.
`has_call_reference` requires `source == ai_call` on that row, so pre-fix it returned False,
the focus block was skipped and the call dispatched FULL. It came out `retry`.

**The narrowing was real, not just a label** — 44 answers over 6 sections against 152 over 16:

| section | call 1 (full) | call 2 (retry) |
| --- | --- | --- |
| infertility_treatment | 73 | 0 |
| diagnostic_testing | 33 | 24 |
| general_coverage | 12 | 0 |
| benefit_coverage | 8 | 0 |
| insurance_information | 8 | 0 |
| enrollment | 4 | 0 |
| pharmacy_benefit_manager / infertility_specialty_pharmacy | 3 / 3 | 0 / 0 |
| authorization_department / third_party_administrator | 2 / 2 | 0 / 0 |
| insurance_representative | 2 | **4** |
| embryo_cryo_storage | 1 | 0 |
| patient_verification | 1 | **1** |
| deductibles / out_of_pocket / lifetime_maximum | 0 / 0 / 0 | **5 / 4 / 6** |

~108 confirmed answers were not re-asked. The six sections it covered are exactly the six
`just seed-retry-form` reports for an equivalently-gapped form on this schema (predicted 45
leaves; 44 answers landed here). That was a separately seeded form, so the matching SECTION SET
is the meaningful agreement — not the leaf count.

**The bookends held.** `insurance_representative` (4) and `patient_verification` (1) are the
`collected_per="call"` leaves, and call 2 captured its own reference at 01:51:57 — so the NEXT
retry's gate is intact. That was the thing most at risk when `bookend_paths` was deleted in
favour of the per-call marker.

**A second behaviour, for free.** `retry_count` is 0 — this was a manual requeue, which resets
the budget. Under the pre-branch rule that alone would have labelled the call `full`. Getting
`retry` also exercises spec D4 (mode follows what was staged, never `retry_count`).

Form landed `completion_pct 100.00 / verified_pct 100.00 / review_reason ready_for_review`.

**What this call does NOT show:** anything about what the agent *said*. The database records
what was collected, not whether the opening announced a prior call. One call, one schema.

### 8.3 Mutation evidence

| mutation | result |
| --- | --- |
| gate additionally requires the CURRENT row to be `ai_call` (the old bug) | the new dispatcher test fails `full != retry`, and is the only failure in the file |
| `_reference_field_for` returns `None` | `test_an_attempt_with_no_reference_number_is_flagged_unauthoritative` fails — which also proves the new `->>` returns the path unquoted against the real DB |
| register one catalog whose reference section drops the marker | 3 whole-catalog invariants fail; **before** the registry change, none did |
| `load_verified_fraction` ignores the caller's `values` | the new value-gated contract test fails `1.0 != 0.5` |
| `review.py` ai_call branch → `ai_supported is not False` | the new no-judge consequence test fails, alongside the existing predicate test |
| `setProvenance({})` reinstated in `closeForm` | the new reopen test reports `absent` instead of `false` |

### 8.4 Gates — head `9da02279`

| gate | result |
| --- | --- |
| `just check` | **2726 passed**, 3 skipped, 21 deselected, 1 xfailed — **0 failed, 0 errors** |
| `mypy --strict` | clean, 399 source files |
| `ruff check` + `ruff format --check` | clean |
| frontend `tsc -b` / `eslint .` / `npm test` / `npm run build` | clean / clean / **671 passed** (91 files) / clean |
| live gate | **PASSED** — §8.2 |

Test-count deltas, all accounted for: backend −3 (`TestHasCallReference`, whose three intents
were already covered on `load_authoritative_call_ids`, including the superseded-reference case)
+3 new; frontend +1 (reopen) −5 (`completionPercent`).

### 8.5 Rejected, and why

The review reported that the Observer dedup change makes an intake-satisfied field unsatisfied
when no judge runs. The mechanism is real but it is neither new nor unpinned — it is the
accepted trade-off in spec §3.5 and is pinned at `test_retryable_fields.py`. Not relitigated.
What *was* added is the weaker version: the existing test pins `is_field_satisfied` in
isolation and never reached the consequence, that the field lands back in
`unsatisfied_required_paths`, which is the auto-complete gate.

The review also asked for a `FormSchemaDoc` validator requiring the reference-number leaf to be
`role="ask"` under an effective `collected_per="call"`. **Not added, deliberately.**
`model_validate` runs on every dispatch against the PINNED schema version, and every version
published before this branch added the marker has no declaration to find — a hard validator
there fails dispatch for existing forms, which is exactly why
`test_every_catalog_marks_its_reference_number_leaf` documents itself as a catalog test. Its
single assertion already covers both halves the validator was meant to check, since
`collected_per_call_paths()` yields only `role="ask"` leaves with effective `collected_per`
`"call"`. The real hole was next door and is fixed: seven whole-catalog invariants iterated a
hardcoded builder pair while `SCHEMAS` is the registry a new insurance type is added to. If a
parse-time check is still wanted, publish-time validation is the safe home.

### 8.6 Two gate holes this round exposed

- **`scripts/` is outside every gate.** Deleting `has_call_reference` broke
  `scripts/seed_retry_form.py` — the one command needed to set up §8.2's live gate — and
  nothing caught it: `scripts/` is not in `[tool.mypy] files`, and ruff cannot see an import of
  a name that does not exist. Adding `scripts` to mypy costs **4 pre-existing errors in 3
  files** (a missing `types-qrcode` stub and a `totp.now()` attribute in `mfa_qr.py`, a
  `no-any-return` in `intake_scenarios.py`) and would have caught this. Not done — it changes
  the gate config and touches unrelated scripts. Verified instead by executing all 12 modules
  under `scripts/`.
- **`ruff check` passing says nothing about `ruff format --check`.** The first `just check` of
  this round failed on formatting alone, on a file that had passed `ruff check` — it came back
  from a mutation backup taken before its format pass. Caught only because the gate was run
  verbatim, exactly as this repo's `CLAUDE.md` warns.

### 8.7 Still outstanding after this round

- `sweep_stuck_ai_processing` still carries `auto_retry_enabled` and `review_floor`; neither is
  passed by any caller and neither is read. `review_floor` was added by this branch and
  orphaned by `05087823`, and the `REVIEW_CONFIDENCE_FLOOR` import at `post_call.py:39` exists
  only to feed its default. `PipelineSweeper._form_auto_retry_enabled` and
  `WorkerEventConsumer._form_auto_retry_enabled` are likewise assigned and never read, while
  `main.py` still threads the setting into both constructors. §4's claim is accurate as far as
  it goes — those parameters WERE removed from `resolve_ai_processing` — but it does not extend
  to its sibling in the same module. Not on the confirmed fix list, so left alone.
- `verified_pct` semantics changed earlier in this branch with no backfill. Rows written before
  it carry the old definition and are now surfaced on `PatientFormDetail`; they self-heal on
  the next eval or dispute-resolve.
- A payer that never issues a call reference number reads 0% verified however complete, so with
  auto-retry ON it would redial to `max_retries` and each retry would run FRESH. Latent while
  the deployment switch is off; worth confirming the payer set before flipping it.
- `load_authoritative_call_ids` applies no judge, confidence or format check to the reference
  answer — any `ai_call` row at that path makes the call authoritative.
