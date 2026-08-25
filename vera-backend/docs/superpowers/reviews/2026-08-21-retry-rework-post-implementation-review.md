# Retry-call rework — post-implementation review

**Date:** 2026-08-21
**Branch:** `fix/retry-calls`
**Spec:** `../specs/2026-08-21-retry-call-scoping-design.md`
**Plan index:** `../plans/2026-08-21-retry-rework-index.md`

Everything here is a decision I took on Tapu's behalf while he was away, or a question the
implementation surfaced that is his to answer. Nothing in this file is a blocker for the code
already committed — it is the record he asked for so he can rework whatever I got wrong.

Status of the work is tracked in the plan ledgers under `.superpowers/sdd/<plan>/progress.md`
(git-ignored scratch; the git history is the durable record).

---

## 1. Decisions I made that were his to make

### 1.1 Deleted the orphaned v1 schema artifact
`data/form_schemas/ibv_form_standard.json` was removed (commit `82a0a9f3`). He approved this
explicitly ("yes cleanup") after I raised it. It was absent from `manifest.json`, referenced
nowhere in the repo, and does not parse as a `dsl_version` 2.1 document — it broke every glob over
`data/form_schemas/`.
**Cost if wrong:** the file is one `git revert` away.

### 1.2 `/simplify` runs once over the whole branch, not per task
The repo rule in `CLAUDE.md` says to run the code-simplifier after every implementation. Each plan
also puts it in its last task. I run it ONCE over the whole branch at the end instead.
**Why:** it is a whole-branch quality pass, and running it 15 times would churn the same files
while later tasks are still landing on them.
**Cost if wrong:** a quality pass lands later than the letter of the rule. Plan A's simplify pass
did run in-task and found one real thing (see 3.1), so the mechanism works either way.

### 1.3 Ran Plan A's `/simplify` review inline instead of via its four agents
The `/simplify` skill dispatches four parallel review agents. At the time, this session was
configured not to launch agents unrequested, so I applied the same four angles (reuse,
simplification, efficiency, altitude) myself to a small mechanical diff.
**Cost if wrong:** less independent coverage on Plan A's diff than the skill intends. Plan A's diff
was ~120 lines of additive DSL code; the pass still found the finding in 3.1.

### 1.4 Test-location and fixture substitutions where the plans were placeholder-only
Two plans sketched tests with invented fixtures or literal `...` placeholders:
- **Plan B Task 1** sketched `session, form, make_call` fixtures that do not exist. Written against
  the real `tests/integration/conftest.py` instead, following `test_load_field_status.py` (the exact
  neighbour — same module under test).
- **Plan B Task 5** sketched a new `tests/integration/test_retry_dispatch.py` of `...`
  placeholders. Written as unit tests in the existing `TestCallPlanStaging` class in
  `tests/unit/services/test_queue_dispatcher.py`, which already models the dispatcher end-to-end
  with fakes and already exercises the focus path.
In both cases the plan's ASSERTION SEMANTICS were treated as binding and only the plumbing changed.
**Cost if wrong:** Task 5's focus wiring is covered by fakes rather than a real session. The real
session path is still covered by Task 1's integration tests and by Plan C's live retry.

### 1.5 Batched Plan B Tasks 3 and 4 into one implement-and-review cycle
Task 4's `focus_paths` cannot be written or tested without Task 3's new parameter — its first
statement calls `_required_paths(..., include_defaulted=True)`. Both acceptance numbers were
pre-verified, so a combined review still gated each independently. It came back with zero findings.
**Cost if wrong:** a larger review surface per round; a fix loop would have covered more code.

### 1.6 Amended a test the plan specified, because the plan's version was wrong
`test_one_missing_group_member_pulls_in_its_whole_panel` asserted 32 paths but its setup produced 0.
Detail in 2.1.

---

## 2. Defects found in the plans themselves

### 2.1 A plan-specified test was wrong (Plan B Task 4)
The brief's `values={target: "$25"}` never satisfied the target leaf's gate chain, so
`sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.copay` was inapplicable and
`focus_paths` correctly returned 0 rather than the asserted 32.

Verified independently, twice (by me and by the reviewer, from source): `copay` carries exactly two
gates — its own `covered == "Yes"` service-field gate, chained under the `cpt_58340` group's
`RefCondition(ref='diagnostic_testing_covered')`, which resolves to
`sections.diagnostic_testing.diagnostic_testing_covered == "Yes"`. The enclosing
`labs_xray_ultrasound` group carries no `applicable_when`, so no third value is needed. Adding
exactly those two values yields 32 = 8 codes x 4 collectable fields. Nothing in `focus_paths`,
`_required_paths` or `expand_to_groups` was loosened to reach it.

**This also corrected me.** I had pre-verified that 32 collectable leaves exist under that panel and
told the implementer that a failure would mean an implementation defect. The COUNT was right; the
test's SETUP was the broken part. Recorded in the ledger so the record is not misleading.

### 2.2 Plan A's verification command was too broad
Its Step 7 globbed `data/form_schemas/*.json`, which picked up the orphaned v1 artifact (see 1.1)
and crashed on it. Replaced with a manifest-driven check. Fixed at the root by 1.1.

### 2.3 Plan A described an import that already existed
It instructed adding `from uuid import UUID` to `review.py`; it was already at line 20. Harmless
staleness.

---

## 3. Findings worth keeping

### 3.1 Plan A: the marker resolution was re-implementing an existing idiom
As specified, `_effective_collected_per` walked UPWARD from each leaf through a prebuilt dict of
every node, using index arithmetic over path segments. `conditions.py:leaf_gates` already
establishes the module's idiom for inheriting an attribute down the tree: one downward walk
threading the value. Rewritten that way — the private helper, the node dict, the `Mapping` import
and a `range(len(parts) - 1, 2, -1)` off-by-one all disappeared, with identical results on all
7 resolution tests and both real artifacts. Commit `639c40e9`.

### 3.2 Deleting `bookend_paths` fixes a real over-asking bug, not just dead weight
Measured on both catalogs by compiling each plan and comparing:

| catalog | `bookend_paths` | `collected_per="call"` | tasks retained by each |
| --- | --- | --- | --- |
| `infertility_treatment` | 3 paths | 3 paths (identical set) | `{introduction, wrap_up}` — same |
| `disease_only` | **15 paths** | 3 paths | `{policy_basics, wrap_up}` — same |

Task retention — the only thing the heuristic existed to guarantee — is preserved exactly. For IBV
the substitution is literally set-equal. The 12 extra paths `bookend_paths` was keeping on
`disease_only` are all FORM FACTS (`plan_name`, `plan_type`, `effective_date`, `group_number`,
`policy_number`, `benefit_year_type`, `renewal_date`, `annual_benefit_maximum`, `policy_state`,
`state_mandate`, `insurance_provider_name`, `insurance_phone_number`) — re-asked on every focused
`disease_only` retry purely because they happened to sit in the first or last task.

### 3.3 `mypy --strict` covers `tests/` in this repo
Plan B Task 2 shipped a `just check` gate failure because the implementer scoped mypy to the
production module and the new test file's helper was untyped. Caught by review before it could pile
up under four more tasks. Root cause was my own dispatch prompt; corrected for all later dispatches
to require mypy on every changed file by name.

### 3.4 The review layer is not infallible either
The reviewer that caught 3.3 proposed `supported: bool = True` as the fix — which would not compile,
because one test passes `supported=None`. Corrected to `bool | None` in the fix message rather than
burning a round on it. Worth knowing when reading these reviews.

### 3.5 Integration tests need `just test`, never bare `uv run pytest`
`justfile:6` sets `dotenv-load := true`, which exports the dotenv file into the recipe environment
as real process vars. `tests/integration/conftest.py:45` calls `Settings(_env_file=None)`, which
deliberately ignores that file and reads only real env vars. So `just test` reaches the branch
database and a bare `uv run pytest` silently falls back to the wrong one (a stale `vera_test` with
an out-of-date `alembic_version`). `just check` is unaffected — it routes through `just`.

---

## 4. Open questions for Tapu

### 4.1 Should an intake value satisfy a confirm-role leaf for the READY_FOR_REVIEW gate?
**Carried from the plan index, deliberately unplanned by every plan.** `unsatisfied_required_paths`
keeps `is_field_satisfied`, so a form whose member ID came from intake and was never read back to
the payer can still route to READY_FOR_REVIEW — "nothing is wrong, just sign it off". Spec D8 fixes
this for the retry ask set and D9 for `verified_pct`, but the human sign-off gate still trusts
intake.
**Why nobody decided it:** it is a compliance-flavoured decision about what a reviewer is being
told, not a retry defect. Per the repo's own rule, compliance determinations do not get invented to
unblock work.

### 4.2 `retry_fill_threshold` wants revisiting (Plan D)
Once both percentage gates measure the same population, `0.5`-of-all-required is not
`0.5`-of-askable. Plan D flags this and deliberately does not change the default, because moving
the measurement and the threshold in one commit would make the behaviour change impossible to
attribute.

*(Sections below are appended as later plans land.)*

---

## 5. Plan B — what the reviews caught (appended as B landed)

### 5.1 A deletion I sanctioned lost coverage I claimed it preserved
I approved deleting `TestBookendPaths` from `tests/unit/forms/test_call_plan.py` on the explicit
grounds that its intent survived in Task 5's new "introduction and wrap_up survive narrowing" test.
A mutation-testing review disproved that: the new test stayed GREEN with the feature removed, under
two separate mutations (deleting the `| doc.collected_per_call_paths()` union from `focus_paths`,
and reverting the dispatcher gate to `call_mode == CallMode.RETRY`).

The deleted test covered the case where the bookend fields are **already satisfied** — the only case
in which the `collected_per="call"` union does any work. The replacement's fixture confirmed just
one of the three per-call leaves, so the other two entered the focus set through `_required_paths`
regardless and the union was never exercised.

Fixed by confirming all three per-call leaves in the fixture, and the implementer was required to
run the mutation itself and show the test failing before restoring the line.

**Lesson:** "the intent survives elsewhere" is a claim that has to be mutation-tested, not asserted.
I asserted it.

### 5.2 Plan B would have introduced a regression on never-dialed forms
Removing the `call_mode == CallMode.RETRY` precondition (spec D4) exposed a latent looseness in
`has_call_reference`: it tested only that SOME current answer existed at the reference path, from
any source. But `PUT /patient-forms/{id}` writes `source=human, call_id=None` for any path present
in `body.form_data`, reference field included.

Consequence, measured: an operator who types a reference number into the form makes that form's
FIRST EVER call dispatch focused — staging 48 of 182 IBV paths and silently dropping every optional
leaf. The old precondition masked this; the spec did not consider it.

**Ruling: fixed rather than deferred**, because leaving it makes Plan B strictly worse than the bug
it fixes for never-dialed forms, and restoring the `retry_count` guard is impossible — that
counter's unreliability is the entire premise of the plan. The fix makes `has_call_reference`
require `source == ai_call` and `call_id is not None`, which is what spec D8 already demands of
`load_authoritative_call_ids`. One production caller, so it is contained.

**Considered and rejected as too large for a fix round:** gating instead on
`load_authoritative_call_ids(...)` being non-empty. That is arguably the better design — it reuses
the exact D8 predicate, has no `is_current` filter so it also survives a human EDITING a
call-captured reference, and would delete `has_call_reference` entirely. Recorded here as a future
simplification.

**Accepted limitation:** if a call captures a reference and a human later edits that field, the gate
reads False and the next call runs FULL. Conservative and self-healing.

### 5.3 Deferred minors from Plan B, for triage before merge
1. The dispatcher focus block sits outside the 4c `try/except`, so a DB error in the two new reads
   now aborts the whole dispatch pass rather than one form. Previously this risk existed only on
   retries.
2. `if focus:` stages the FULL plan when the focus set is empty. Impossible on IBV; latent for a
   schema that declares no `collected_per="call"` leaves.
3. `assert form.retry_count == 0` in the focus test can never fail — it documents intent; the subset
   assertion is what actually proves the gate moved off the counter.
4. Tests (a) and (c) in `TestCallPlanStaging` are byte-identical apart from the fix in 5.1.
5. **Worth real attention:** `load_field_status` embeds `latest_eval_subquery()`, a `GROUP BY
   answer_id` over the WHOLE `field_evaluation` table with no form filter. It now runs once per
   DISPATCHED FORM instead of once per retry. Pre-existing, but this change widened its blast
   radius considerably. A form-scoped correlation on the subquery is the fix.
6. `FormSchemaDoc.model_validate` re-parses per form, though `_resolve_plan_template` already parsed
   the same document; `schema_versions` memoizes only the row, not the parsed doc.

---

## 6. Plan C — the defect the branch exists for is closed

### 6.1 The headline result
The spec measured the defect as: a focused retry narrowed `tasks 9 → 4` and `fields 182 → 45`, but
`questions 25 → 25` and `prompt 7123 → 7123 chars, byte-identical`. It re-asked every question of
every surviving task, making it strictly worse than a full call — it lost the read-back and kept
every question.

Measured end to end through the dispatcher on the seeded scenario after Plan C:

| task | FULL q | FOC q | FULL chars | FOC chars |
| --- | --- | --- | --- | --- |
| `introduction` | 1 | 1 | 1578 | 1578 |
| `diagnostic_coverage` | 4 | 3 | 1829 | 1756 |
| `financial` | 18 | 10 | 3199 | 1914 |
| `wrap_up` | 2 | 2 | 517 | 517 |
| **totals** | **25** | **16** | **7123** | **5765** |

`introduction` and `wrap_up` are byte-identical BY DESIGN — their only fields are the
`collected_per="call"` leaves, always in the focus set. That is precisely the mechanism that
replaced the deleted `bookend_paths` heuristic, so seeing them unnarrowed is confirmation.

### 6.2 Plan C quoted the wrong column of its own spec
Its acceptance sentence predicts `diagnostic_coverage` goes "4 spoken questions → 1". The measured
value is 3, and **3 is correct**. The spec's evidence table has columns
`[entry prompt Qs | owed_now | gap_fields | gap list Qs | +explode]` and the `diagnostic_coverage`
row reads `4 | 1 | 8 | 1 | 3`. The implementation deliberately uses
`focus_questions(..., explode=True)` — mandatory, because an agent holding an answer with no
sanctioned next question invents one — so the predicted value is the `+explode` column. Read against
the right column the measured table matches the spec **exactly on all four tasks**: 1 / 3 / 10 / 2.

No action taken. Recorded so the next reader does not "fix" a correct number.

### 6.3 A dead parameter the plan itself introduced
`focus_call_plan`'s specified signature took `doc: FormSchemaDoc` as its first argument, but the
plan's own specified body reads `shared = plan.shared_conditions` and never touches `doc`. Ruled a
plan defect and dropped; the real signature is `focus_call_plan(plan, paths, *, answers)`.
`plan.shared_conditions` is the better source anyway — the compile-time snapshot belonging to that
exact plan, with no doc/plan mismatch risk.

**Consequence to note:** Plan C's File Structure section still documents the `doc` parameter, so that
part of the plan no longer matches the code.

### 6.4 The vacuous-test pattern, and what I changed because of it
Four times on this branch a test shipped that passed with its feature removed or asserted something
trivially true:

1. **B2** — a test helper untyped, so `mypy --strict` (which covers `tests/` here) failed only at
   the full gate.
2. **B5** — the "greeting and wrap-up survive narrowing" test passed with the
   `collected_per="call"` union deleted AND with the dispatcher gate reverted. I had personally
   sanctioned deleting the older test on the grounds its intent survived here. It did not.
3. **B5 (again)** — tightening `has_call_reference` silently voided a PRE-EXISTING focus test whose
   fixture lacked a `call_id`: it began staging the full 182-path plan and still passed, because its
   assertions were trivially true of the full plan.
4. **C1** — `test_fields_and_panels_narrow_to_the_same_set` asserted `tracked <= spoken`, which
   holds trivially. Reversing the derivation to reintroduce the original defect left the ENTIRE
   suite green while 37 questions became invisible to `owed_now`.

Every one was found by mutation testing, not by reading. **I now require reviewers to mutation-test
key assertions by default rather than on request**, and I fold vacuity guards into the same fix
round rather than deferring them. The two guards added in C1's fix round — a non-zero
`still_needed` count, and a byte-level `model_dump_json` purity compare — exist because of this
pattern.

### 6.5 Deferred minor from Plan C
The `not task.panels` branch in `focus_call_plan` is correct but currently DEAD: no task in either
catalog has empty panels. It is reachable only for a task whose collectables are all end-of-task
confirms, and if reached, such a task would keep ALL its fields and become undroppable — which
would silently break `test_empty_focus_yields_no_tasks`.

---

## 7. Plan D — and a number that changes the threshold question

### 7.1 What landed
Both percentage gates now measure only what a call can fill. Measured on the seeded scenario:
`completion / verified` moved **94.12% / 92.68% → 93.51% / 91.95%**, and the spec predicted ~91.95%.
The frontend mirror was updated to match (`completionPercent`, 13% against the backend's 12.82% —
the frontend rounds to whole numbers).

The review ran **six mutations** and hash-verified that `is_field_satisfied`,
`unsatisfied_required_paths` and `retryable_required_paths` are byte-identical to branch start across
three revisions. Mutation A confirmed the spec's inseparability claim exactly: dropping
`askable_only=True` caps `verified_pct` at **0.9085**, the predicted 90.9%.

### 7.2 A brand-new form does NOT read 0% — it reads ~45%
Plan D's verification section claims "a brand-new IBV form reads 0% complete rather than 30.6%".
**Both numbers are wrong.** Measured on the actual seeded `TEST-SEED-READY` form after the fix:

| bucket | count | why it counts as filled |
| --- | --- | --- |
| relevant askable-required leaves | 47 | the new denominator |
| filled by a declared `default` | 7 | spec §4.4 — a default counts filled |
| filled because **intake** supplied them | 14 | value presence is `completion_pct`'s definition |
| genuinely unfilled | 26 | |
| **never-called form reads** | **44.68%** | |

Twelve of those fourteen are **`role="ask"`** leaves — `plan_type`, `coverage_type`, `cob_status`,
`benefit_year_type`, `plan_effective_date`, `plan_year_information`, `plan_fund_type`,
`employer_support_size`, `infertility_plan_mandate`, `doctor_inside_network`,
`facility_inside_network` — plus `policy_number` (confirm). Two more (`rep_name`,
`call_reference_number`) are seeder artifacts that a genuinely never-called form would not have, so
the honest figure for a real intake-only form is ~40%, matching the reviewer's independent 40.0%
measurement of the pre-fix state.

**This is not a defect.** `completion_pct` is defined as VALUE PRESENCE (spec D9's own table), and
those values are genuinely present. Plan D correctly removed the *non-askable* constant offset. What
remains is intake filling askable leaves, which is real data.

### 7.3 Why that matters for `retry_fill_threshold` — the decision to make
`post_call.py:93` computes `low_fill = form.completion_pct < tenant.retry_fill_threshold * 100`. At
the seeded threshold of **0.5**, a never-called form already sits at **44.68%** — just 5.3 points
below being parked as good enough. A clinic that fills a few more intake cells could push a form over
0.5 and have it **parked without ever being called**.

Plan D deliberately did not change the default, and that was right — moving the measurement and the
threshold in one commit makes the behaviour change impossible to attribute. But the decision is now
sharper than the plan framed it:

- `verified_pct` (which uses `is_call_confirmed`) reads **0%** on that same never-called form, because
  nothing was authoritatively confirmed. It is the honest "has a payer rep actually told us this"
  number.
- `completion_pct` reads 44.68% and trusts intake.
- **`post_call.py` gates the retry on the one that trusts intake.**

So the open question is not only "is 0.5 still the right number" but "**should the retry gate compare
against `completion_pct` at all, rather than `verified_pct`?**" That is a product decision and is
deliberately left to you.

### 7.4 A dev-seeder gap found while verifying this
`scripts/seed_patient_data.py:309` hardcodes `completion_pct=0` on the `PatientForm` and never calls
`recompute_form_projection`. So `TEST-SEED-READY` stores 0.00 while its true
`completion_pct_v2` is 44.68% — the worklist shows 0% for a form that is 45% filled.

Pre-existing and dev-only (production answer writes go through `recompute_form_projection` at
`field_answers.py:161`), but it means Plan D Step 2's "re-seeding rewrites both" is **not** true for
that form. Worth a three-line fix.

---

## 8. A schema asymmetry the exhaustive tests surfaced

Writing Plan F's existence-flag scenarios turned up an inconsistency in
`ibv_form_standard_v2` that has nothing to do with retries — it is a schema authoring gap:

| section | leaves collected |
| --- | --- |
| `pharmacy_benefit_manager` | `pbm_exists`, `pbm_name`, **`pbm_phone`** |
| `infertility_specialty_pharmacy` | `isp_exists`, `isp_name`, **`isp_phone`** |
| `third_party_administrator` | `tpa_exists`, `tpa_name` — **no phone** |
| `enrollment` | `enrollment_required`, `enrollment_provider_name`, `enrollment_provider_phone`, `center_of_excellence_required` |
| `authorization_department` | `auth_department_name`, `auth_department_phone` |

Every other third-party contact in the form collects a phone number. The TPA collects only a name.
If a biller needs to reach the third-party administrator, that phone number is exactly what they
would want, and the call never asks for it.

**Not touched by this branch** — adding a leaf changes the schema, republishes a `schema_version`,
and needs a re-seed, which is out of scope for a retry-scoping change. Flagging it as a product
question: is the missing `tpa_phone` deliberate, or an omission worth a follow-up schema change?

(My own Plan F plan text asserted `tpa_name, tpa_phone` by pattern-matching the PBM and ISP shapes.
The implementer checked the compiled artifact, found no such leaf, sized the test to the real
single-leaf shape and documented it. The plan text was wrong; the code and the test are right.)

---

## 9. Two environment traps that cost real time — worth knowing before the next branch

Both looked like regressions from this work and neither was. Recorded because both will recur.

### 9.1 A running Langfuse stack inflates `just check` by 3.4x
Measured on identical code:

| condition | full pytest |
| --- | --- |
| Langfuse compose profile running | 619.92s, then 712.40s |
| Langfuse stopped | **182.79s** |
| baseline before Plans D/E/F | 172-178s |

`docker stats` during the slow runs: a 3.827GiB Docker VM with Langfuse holding ~2.4GiB of it
(clickhouse 1.401GiB, web 477MiB, worker 346MiB, minio 170MiB).

What made it diagnosable was that the **sum of the parts never showed the slowdown** — `tests/unit/`
30s, `apps/` 32s, `tests/integration/` 124s, and all 553 `tests/unit/forms/` (including Plan F's new
35) just **3.73s**. A code hot spot cannot hide from a per-directory decomposition; environmental
starvation can. `CLAUDE.md` already warns the stack "balloons the Docker VM until the kernel
OOM-kills its ClickHouse — start it only when you actually need tracing". It is right.

**Trap inside the trap:** `just langfuse-down` is NOT scoped to the profile.
`docker compose --profile langfuse down` tears down the whole compose project, so postgres, redis,
livekit and sendria go with it. Recoverable with `just up` (volumes survive without `-v` — the branch
database and both seeded forms were intact), but the recipe name promises a scope it does not have.

### 9.2 Test-DB residue breaks the invite tests, and looks nothing like residue
`tests/integration/control_plane/test_admin.py::test_invite_records_inviter_and_role_grant_provenance`
failed with `assert invited_by == admin_id` over two unrelated UUIDs. It failed identically in
isolation, so it was not a concurrency artifact (my first theory, and wrong). `vera_retry_call_fix_test`
held **34 stranded `user_identity` rows** from interrupted runs, so the test picked up a stale admin.

Remedy, and it is cheap: `DROP DATABASE vera_retry_call_fix_test WITH (FORCE)`. The session fixture
at `tests/integration/conftest.py:60` recreates and migrates it on demand. After the drop, all 53
tests in that file pass.

**Final gate on a clean test DB with Langfuse stopped: `2662 passed, 3 skipped, 21 deselected,
1 xfailed` in 182.79s, exit 0.**

---

## 10. Plan F — the guarantee that replaced the LLM judge

Requested directly after the eval harness proved unreliable. `focus_paths` is a pure function, so the
retry ask set can be pinned exactly rather than graded. 35 scenario tests, in one module.

**Four global invariants, asserted on every scenario:**

| | guarantee |
| --- | --- |
| I1 soundness | every path in the focus set is collectable and either applicable, or explained by an owed sibling in its own group |
| I2 completeness | every required ∧ applicable ∧ collectable path no authoritative call confirmed IS in the set |
| I3 group closure | if any member of a group is in the set, all its applicable members are |
| I4 call-scoped | the three `collected_per="call"` paths are always present |

Each was proven to FAIL under a targeted mutation (drop the applicability filter; flip
`include_defaulted`; drop `expand_to_groups`; drop the call-scoped union). The reviewer re-ran three
independently. It also showed the named-case assertions bite on their own: dropping the applicability
filter breaks 13 of 19, forcing `_confirmed` false breaks 9 of 19.

**Coverage** — every case asked for, plus what discovery turned up: spouse details via
`family_coverage`; PCP referral via `plan_type == "HMO"`; the two-level male-partner gate; TPA / PBM /
ISP / enrollment as four existence flags; the **27-way** `any_service_requires_prior_auth` rollup
tested through two different disjuncts so it proves a real `any`; money sentinels across all three
triplets; the 16 either/or blocks; and group closure on the 32-member `labs_xray_ultrasound` panel.

**The three extremes** are the strongest assertions:

| scenario | focus set |
| --- | --- |
| every leaf answered AND authoritatively confirmed | **exactly 3** — the call-scoped paths, nothing else |
| every leaf answered by a NON-authoritative call | 162 — nothing trusted, nothing skipped |
| every leaf supplied by intake | 162, identical set |

### 10.1 My invariant was wrong, and the code was right
I specified I1 as "every path in the focus set is applicable under the scenario's values". That is too
strong. `expand_to_groups` pulls in a group's members whenever ANY member is owed, without re-checking
each member's own gate — so not-yet-applicable siblings legitimately appear.

That behaviour is not just acceptable, it is necessary, and I verified both halves:
1. the carried-forward questions **render with their condition attached** — measured on a focused plan
   whose only owed leaf was the ungated `cpt_99211.covered`, the prompt reads
   "2. What is the copay or coinsurance…? — **Ask only if this service is covered.**";
2. excluding them would be a bug — `focus_call_plan` drops non-focused paths from BOTH `fields` and
   `panels`, so the moment the rep says "yes, covered" the agent would have nothing sanctioned left to
   ask and would either invent a question or silently lose copay/coinsurance/prior_auth.

**So the guarantee is sharper than "only applicable paths":** the focus set never contains a path from
a branch the form has RULED OUT, and every carried-forward path renders with its condition. That
distinction is what a reviewer should hold in mind when reading the suite.

### 10.2 Known coverage boundary
`Scenario` carries a single `confirmed_by_authoritative` bool, so **mixed per-call confirmation** —
call 1 authoritative over part of the form, call 2 not, which is the commonest real retry — is
unrepresentable. Extending it requires I1/I2 to model per-path provenance, which is a deliberate
design change rather than a fix. Documented in the module docstring. **This is the suite's real limit
and the most valuable thing to extend next.**

Also: I1 and I2 can FALSE-FAIL on correct behaviour outside the 35 scenarios (I1 on an ordinary
partially-covered panel, because its exemption checks only the innermost group where
`expand_to_groups` sweeps ancestors; I2 on either/or, because `_owed_now` does not model alternative
satisfaction). Both fail in the SAFE direction — false failure, never false pass — and both are now
commented in place, because the next person to add a scenario will hit one and might weaken the
invariant instead of the scenario.

---

## 11. The pattern that dominated this branch: tests that could not fail

**Eight times** a test shipped passing with its feature removed, or asserting something trivially
true. Every one was found by mutation testing, never by reading:

| # | where | what passed with the feature gone |
| --- | --- | --- |
| 1 | Plan B T2 | helper untyped, so `mypy --strict` (which covers `tests/` here) only failed at the full gate |
| 2 | Plan B T5 | "greeting and wrap-up survive narrowing" passed with the `collected_per` union deleted AND with the dispatcher gate reverted |
| 3 | Plan B T5 | tightening `has_call_reference` silently voided a PRE-EXISTING focus test whose fixture lacked a `call_id` — it began staging the full 182-path plan and still passed |
| 4 | Plan C T1 | `test_fields_and_panels_narrow_to_the_same_set` asserted `tracked <= spoken`, trivially true; reversing the derivation left the ENTIRE suite green while 37 questions went invisible to `owed_now` |
| 5 | Plan D | `test_applicability_gates_the_denominator` collapsed to 100/100/100 — its own comment admitted `relevant` was empty "regardless of applicability" |
| 6 | Plan E T1 | the production wiring had no coverage at all: neutering `_call_scoped_paths` left all 560 control-plane tests passing |
| 7 | Plan E T3 | deleting the ENTIRE `FieldRow` pill block left 637/637 passing, because the test file mocks `provenanceFor: () => undefined` |
| 8 | Plan E T4 | the schema-title test asserted `toContain("Total")`, which the FALLBACK breadcrumb also contains — neutering the schema lookup left 14/14 green |

**What changed because of it:** I stopped asking reviewers to mutation-test key assertions and started
requiring it, and I began folding vacuity guards into the same fix round rather than deferring them.
Case 2 is the one worth remembering — I had personally approved deleting an older test on the grounds
its intent survived in a new one. It did not, and only a mutation showed that. **"The intent survives
elsewhere" is a claim to be tested, not asserted.**

---

## 12. Plan text that was wrong, and how it was caught

My own planning was wrong **five** times about schema detail. Every one was caught by an implementer
checking the compiled artifact instead of trusting the plan:

1. **`tpa_phone` does not exist.** I pattern-matched the shape from PBM and ISP. Only `tpa_exists` and
   `tpa_name` exist — which then surfaced the product gap in §8.
2. **A context-only form does not read 0%.** It reads 12.82%, because 5 askable-required leaves carry a
   `default` and a declared default counts as filled (spec §4.4).
3. **"17 either/or sets"** — there are 16 blocks / 27 exploded pairs. Materially, `copay`/`coinsurance`
   are *themselves* an either/or pair, so the missing leaf I suggested for the group-closure test would
   have been satisfied by its sibling and closure would never have fired. **That test would have passed
   while proving nothing about the behaviour specifically asked for.**
4. **"drops exactly 2 disputes"** — the schema declares three call-scoped leaves; measured 151 → 148.
5. **`focus_call_plan(doc, ...)`** — the plan's own specified body never used the `doc` parameter it
   introduced.

**The lesson is narrow and actionable:** I derived the gate tables in Plan F from the compiled artifact
and every one was right; I guessed leaf names and counts from neighbouring sections and those were
wrong. Constants in a plan should be generated, not written.

---

## 13. What is left

### Needs you
1. **A live browser-callee retry** (Plan C Task 3). The eval harness is not a substitute — see §6 and
   §9. This is the one remaining verification gap on the voice path.
2. **`retry_fill_threshold`** — §7.3. A never-called form reads ~45% complete against a 0.5 default.
   The sharper question is whether the retry gate should read `verified_pct` (0% on such a form)
   rather than `completion_pct`, which trusts intake.
3. **The eval harness goodbye loop** — §9 context, measured in §6: two LLMs exchanging goodbyes for
   ~60% of a run, burning the turn budget and manufacturing failure verdicts. Fix before trusting the
   harness again.
4. **The missing `tpa_phone`** — §8. Deliberate or an omission?
5. **Whether a bare leaf title reads clearly** in the per-attempt view: a deductible total renders as
   "Total", because "Individual Deductible" is the enclosing GROUP's title. A group+leaf label is a UI
   decision, not a bug fix.

### Deferred technical items
- Gate on `completion_pct` vs `verified_pct` in `post_call.py:93` (same as 2 above, but it is a
  one-line change once decided).
- `load_field_status`'s `latest_eval_subquery()` does a `GROUP BY` over the WHOLE `field_evaluation`
  table with no form filter. Pre-existing; Plan B widened when it runs (per dispatched form rather than
  per retry). A form-scoped correlation is the cheap fix.
- `scripts/seed_patient_data.py:309` hardcodes `completion_pct=0` and never calls
  `recompute_form_projection`, so `TEST-SEED-READY` stores 0.00 while its true value is 44.68%.
- The seeder still does not exercise a form carrying a NON-authoritative attempt, nor one whose
  `applicable_when` parent is unanswered — two of the three gaps the plan index listed (the third,
  the missing `CallFormSnapshot`, was closed).
- Mixed per-call confirmation in the Plan F suite (§10.2).
- The `not task.panels` branch in `focus_call_plan` is correct but currently unreachable.
- Minor deferred items are listed per-plan in the ledgers under `.superpowers/sdd/*/progress.md`.

### Environment
Langfuse is currently **stopped** — I took it down while diagnosing §9.1 and left it down, which is the
hygiene `CLAUDE.md` recommends. `just langfuse-up` restores it, or use the `langfuse-adc` skill's
command if you need the LLM playground. Note `just langfuse-down` is not scoped to the profile (§9.1).

---

## 14. Final whole-branch review — verdict and three cross-plan findings

**Verdict: ready with follow-ups, gated on one live focused retry.**

Independently verified against merge-base `afe7d059` (per-function md5, not signature checks):
`is_field_satisfied`, `unsatisfied_required_paths`, `retryable_required_paths`, `_satisfied`,
`_gate_values` and `_alternatives` are **byte-identical**. `bookend_paths` is gone with no positional
`tasks[0]` / `tasks[-1]` assumption left in production code. No model or migration touched. PHI clean
across the union of all six plans — no new log, print, span, URL, query-string or browser-storage
carrier. Final gate on this tree: `2666 passed, 3 skipped, 21 deselected, 1 xfailed` in 176s.

Recorded so nobody "fixes" it: the unguarded `FormSchemaDoc.model_validate` at
`queue_dispatcher.py:407` now runs on every dispatch rather than only retries, but it sits behind
`staged_plan is not None`, and `_resolve_call_plan` returns `None` for a non-v2 schema (form parked
`CALL_FAILED`, loop continues). A legacy schema cannot reach it.

### 14.1 CORRECTION TO §7.3 — my own analysis was wrong
I compared "a never-called form reads 44.68% complete against 0% verified" as if those two numbers
shared a denominator. **They do not**, and the review caught it. Measured on
`ibv_form_standard_v2` with intake-only values:

| number | denominator | why |
| --- | --- | --- |
| `completion_pct_v2` | **39** | keeps the 5 defaulted askable-required leaves and counts them filled |
| `verified_pct` (`satisfied_required_fraction`) | **34** | routes through `_required_paths` with `include_defaulted` defaulting **False**, dropping those 5 |
| `focus_paths` ask set | **39** | passes `include_defaulted=True` — the retry rule |

The five are `telehealth_covered`, `enrollment_required`, `group_name`, `group_number`,
`policy_situs`.

**Consequence, and it is worse than the threshold question I raised:** a focused retry asks **39**
leaves while `verified_pct` measures **34**, so **`verified_pct` can read 100% while a focused retry
still has 5 questions to ask.** Neither function is wrong in isolation — `include_defaulted=True` is
deliberately the retry rule and `False` is deliberately the completeness rule — but the two gates
compared against the same `tenant.retry_fill_threshold` do not measure the same population, which is
exactly the incoherence Plan D set out to remove.

**So the `retry_fill_threshold` decision in §13.2 should wait on this.** Deciding a threshold against
two different denominators is deciding it against nothing. My §7.3 numbers stand as measurements;
the *comparison* I drew from them does not.

### 14.2 `call_mode` still labels and links the call, but no longer describes it
`queue_dispatcher.py:369, 447, 472`. The branch's whole premise is that a manual requeue resets
`retry_count`, so `call_mode` comes out `FULL` — and the focus block now runs anyway, correctly. But
`Call.mode` is still derived from `retry_count`, and the `CallLineage` insert is still guarded by
`if call_mode == CallMode.RETRY:`.

**So the exact scenario this branch exists to fix writes `Call.mode="full"` and no lineage row.** Plan
E's new per-attempt view then reports a narrowed 16-question retry as a full call with
`retry_of=None`. No single task review could see it: Plan B changed only the gate, Plan E only added
the pills.

**Tension to resolve, not a defect to patch blindly:** spec D4 *deliberately* kept `call.mode` on
`retry_count` "for reporting and `CallLineage`", and that decision predates Plan E's view existing. The
options are to derive mode and lineage from the same predicate the focus block uses, or to accept that
`mode` means "was a retry budgeted" rather than "was this call focused" and relabel the UI. **That is
the author's call.**

### 14.3 The greeting/wrap-up guarantee is now data-declared with nothing validating the declaration
`bookend_paths` used to guarantee in CODE that a focused retry keeps its greeting, recording
disclosure and reference capture. That guarantee is now carried by the `collected_per="call"` marker —
and `dsl.py:_validate_document` has no rule requiring any document to declare one.

`_call_scoped_paths` (`patient_forms.py:697`) treats a pre-marker document as "nothing exempt", which
is right for disputes and wrong for `focus_paths`: with an empty call-scoped union, a form pinned to a
pre-marker `schema_version` would get a focused retry with **no greeting, no recording disclosure and
no reference capture**, silently breaking the *next* retry's gate. That is QA issues 3 and 4 returning.

Today nothing is exposed — Plan A's re-seed check confirmed no `patient_form` is pinned to a demoted
version, and the two catalogs are pinned by `test_schema_dsl.py:991` and `:1010`. But those tests
enumerate `build_ibv_standard` / `build_disease_only` **by hand**, so a third insurance type is
unguarded, and the re-seed check was a one-time verification rather than an invariant.

**Tension:** spec D1 explicitly rejected a document-level validator requiring the marker, because
combined with the `role="ask"` restriction it makes ~12 existing fixtures unconstructible. A narrower
rule — e.g. "a task that declares an `intro` must contain at least one call-scoped collectable leaf" —
would likely avoid that collision, but it is a design decision rather than a cleanup.

### 14.4 Minor, from the same review
- **Inconsistent hardening.** `authoritative_calls` is *required* on `satisfied_required_fraction` and
  `focus_paths` (so mypy catches a missed caller) but defaults on `build_field_views`,
  `_open_dispute_paths`, `_unresolved_dispute_count`, `load_call_attempts` and
  `load_field_provenance`. Every production caller is correct today; a future one silently gets
  pre-branch behaviour.
- `doc.collected_per_call_paths() if doc is not None else frozenset()` is written twice —
  `patient_forms.py:698` and inline at `:1135`.
- The **export** path is the last `authoritative_calls=None` caller. Nothing is misstated (the XLSX
  renders neither flag) but the workbook a biller keeps now omits a distinction the UI makes.
- `recompute_form_projection` rewrites `completion_pct` on every answer write and never `verified_pct`,
  against `post_call_eval.py:459`'s "verified_pct must always mirror completion_pct". Pre-existing;
  Plan D widened the gap.
- "Authoritative" defaults **opposite ways by layer**: unknown ⇒ *trust* in `call_provenance.py:88`
  (so a legacy form is not falsely marked unproven), unknown ⇒ *don't trust* in `is_call_confirmed`
  (so a retry re-asks). Each is fail-safe for its own consumer and both are documented, but the word
  now carries two defaults.

### 14.5 On the frontend mirror's cross-check (specifically assessed)
It would catch what Plan D changed: `mock.ts` imports the backend artifact directly rather than a copy,
so both sides read one document; the frontend pins 39/5/13 while the backend derives its expectation,
so removing the role filter fails on either side. What it cannot catch is divergence in a dimension
neither number depends on — rounding (already known) and `isSatisfied`'s alternative-sibling rule,
which is mirrored by hand with no fixture asserting the two implementations agree on one input.

---

## 15. The live focused retry — the merge gate, taken

Call `01a037af-c077-7af0-b266-5587eca6384a`, form `TEST-SEED-RETRY`, armed with
`just arm-retry-form`. 4m41s, `completed`, `mode=retry`, `completion_pct` 100.00.

### 15.1 The retry scoping is verified correct on a live call

| check | result |
| --- | --- |
| questions spoken | **16** — exactly the 16 Plan C predicted (down from 25) |
| greeting + recording disclosure | spoken **once**, at seq 0 |
| `male_partner_coverage` questions | **0** |
| `general_coverage` / office-visit questions | **0** |
| `enrollment` / PBM / TPA questions | **0** |
| the 2 "infertility" mentions | both `lifetime_maximum` questions — in scope |
| rep name + call reference captured | **yes** — the wrap-up task survived the narrowing |
| `CallLineage` row | **written** (parent `01a023e8-…`) |
| snapshot | finalized, 169 → 178 keys |
| answers written | 34 rows, 27 current, across exactly the 5 focused sections |

Every section the retry should have skipped, it skipped. Every section in the focus set, it asked.
**This is the evidence the eval harness could not give, and it confirms P7 is closed in production.**

Note `mode=retry` here, so §14.2's mislabelling did NOT trigger: `arm-retry-form` preserves
`retry_count=1` by design. §14.2's scenario needs the OPERATOR surface
(`PUT /patient-forms/{id}/status`, `manual=True`), which resets `retry_count`. **The finding stands
and remains untested — this call did not exercise it.**

### 15.2 One real defect, and it is NOT the narrowing

`sections.patient_verification.is_insurance_active` — a `collected_per="call"` leaf — was **asked and
answered but never recorded**:

```
seq 2  agent  Great, thank you. Can you confirm the patient's insurance is currently active?
seq 3  user   Yes. It is active.
seq 4  agent  Great, let me pull up my questions...
```

No `field_answer` row exists for that path from this call. The no-op guard at
`services/field_answers.py:110` cannot explain it — it requires `current.call_id == call_id`, and the
only existing row belongs to the seeded prior call, so a write would have happened.

**Ruled out — the narrowing.** Rebuilding this form's focused plan from the live DB state shows the
Plan C invariant held: the path is in the focus set, the `introduction` task is retained, the path is
in `task.fields` **and** in the spoken questions, and `fields == spoken` for that task. The question
was both tracked and asked. This is not a P7-class regression.

**Where it was lost.** The first answer written by the whole call is `06:52:58` in
`diagnostic_testing` — the SECOND task. The call started `06:51:15`. **No extraction produced a write
during the introduction task at all.** `patient_verification` has exactly one collectable leaf, and
it is the one that went missing.

**Why it matters more than one field.** `is_insurance_active` arms the `insurance_not_active` flow
rule, which TERMINATES the call when the policy is not active. That is the exact reason Plan A marked
this leaf `collected_per="call"` — so a retry re-verifies it and a policy that lapsed since the last
attempt ends the call instead of proceeding on a stale "Yes". **The marker did its job (the question
was asked); the extraction did not, so the rule cannot arm.** On this call the rep said "Yes", so
nothing was harmed — a "No" would have been missed the same way.

**What I could not determine.** Why the introduction task's extraction produced nothing. The dev
stack ships no container logs and Langfuse was stopped during this session, so there is no worker
trace to read. Candidates worth checking with tracing on: an extraction window that closes at task
handoff (the agent moved on at seq 4, one turn after the answer), or the Observer's per-task pass not
running for a task whose only collectable field is a flow-rule gate.

**Whether it is new.** Not caused by the narrowing — that is proven above. Whether extraction ever
worked for this leaf on a real call is unknown: the only other row at this path was written by the
seed script, not by an Observer run. Establishing that needs a full (non-focused) call with tracing.

**Recommended next step:** re-run with Langfuse up (`langfuse-adc` skill's command, not
`just langfuse-up`) and take one more retry, then read the introduction task's Observer span. This is
a voice-pipeline defect, separate from the retry-scoping branch, and belongs in its own issue.
