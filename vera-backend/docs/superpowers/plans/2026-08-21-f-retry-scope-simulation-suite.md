# Plan F — exhaustive retry-scope simulation suite

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prove, by simulation over the real schema, that a focused retry asks **every** question it
must and **no** question it must not — so the guarantee no longer rests on an LLM judge's verdict.

**Why:** the call-flow eval harness grades with an LLM and is currently unreliable (a pre-existing
goodbye loop consumed ~60% of one run's turns and manufactured two failure verdicts). Its verdicts
are flaky by construction. The retry ask set, by contrast, is a **pure function** —
`focus_paths(doc, status_by_path, schema_json, *, floor, values, authoritative_calls)` — so it can be
pinned exactly.

**Spec:** `../specs/2026-08-21-retry-call-scoping-design.md`

## Global Constraints

- Every command runs from `vera-backend/`.
- **Unit tests only. No database, no network, no LLM.** `focus_paths` is pure.
- **New file:** `tests/unit/forms/test_retry_scope_simulation.py`. Do not fold these into
  `test_focus_paths.py` — that file covers the predicate's units; this one covers whole-form
  scenarios and is meant to be read as a catalogue of real retry situations.
- **`vera_core/` is not modified by this plan.** If a scenario reveals a genuine defect in
  `focus_paths`, STOP and report it rather than editing production code to make a test pass. That is
  the single most valuable possible outcome of this work and must not be silently absorbed.
- Never log or print a field value. Paths and counts only.
- Code style: PEP 695 type params; ruff rejects `Generic[T]`/`TypeVar`. Line limit 100.
- mypy `--strict` covers `tests/`.

## The anti-tautology rule (read before writing a single assertion)

The suite must not compute its expectations by re-running the code under test. Five tests on this
branch have already shipped passing with their feature removed, so this is not hypothetical.

Two legitimate sources of expectation:

1. **`vera_core.forms.conditions` primitives** — `leaf_gates`, `is_applicable`, `is_required`. These
   live in a DIFFERENT module from `focus_paths`, are separately tested, and encode the schema's
   declared gating. Deriving "which leaves are applicable under these values" from them is
   independent of `review.py`.
2. **Hand-written literal path sets** for the named scenarios, scoped to one section or group so they
   stay readable and honest.

**Forbidden:** calling `focus_paths`, `_required_paths`, `expand_to_groups` or `is_call_confirmed` to
build an expected value. Every invariant below is expressible without them.

## The schema facts this suite is built on (measured, do not re-derive)

`data/form_schemas/ibv_form_standard_v2.json`, 44 groups (collectable members per group:
4×size-3, 31×size-4, 4×size-6, 2×size-10, 2×size-14, 1×size-32), 17 either/or sets.

Named shared conditions:

| ref | meaning |
| --- | --- |
| `family_coverage` | `benefit_coverage.coverage_type == "Family"` |
| `infertility_covered` | `infertility_treatment.infertility_tx_covered == "Yes"` |
| `diagnostic_testing_covered` | `diagnostic_testing.diagnostic_testing_covered == "Yes"` |
| `male_partner_in_scope` | `family_coverage` **AND** `patient_information.spouse_gender == "Male"` |
| `any_service_requires_prior_auth` | a **27-way** `any` over every service's `.prior_auth == "Yes"` |

Field-level gates the scenarios exercise:

| gated leaf(s) | gate |
| --- | --- |
| `benefit_coverage.pcp_referral_required` | `insurance_information.plan_type == "HMO"` |
| `patient_information.spouse_partner_name` / `_dob` | `family_coverage` (role **confirm**, `required when family_coverage`, `default "N/A"`) |
| `male_partner_coverage.male_partner_covered` | `male_partner_in_scope` |
| `male_partner_coverage.{semen_analysis,sperm_cryopreservation}.*` | `male_partner_in_scope` AND `male_partner_covered == "Yes"` |
| `enrollment.enrollment_provider_{name,phone}` | `enrollment.enrollment_required == "Yes"` |
| `third_party_administrator.tpa_{name,phone}` | `tpa_exists == "Yes"` |
| `pharmacy_benefit_manager.pbm_{name,phone}` | `pbm_exists == "Yes"` |
| `infertility_specialty_pharmacy.isp_{name,phone}` | `isp_exists == "Yes"` |
| `authorization_department.auth_department_{name,phone}` | `any_service_requires_prior_auth` |
| `<service>.cpt_*.{copay,coinsurance,prior_auth}` | that CPT's `.covered == "Yes"` |
| `deductibles.individual.{met_amount,remaining}` | `deductibles.individual.total not_in ["$0","None","No Deductible","Unlimited","No Limit"]` |
| `out_of_pocket.individual.{met_amount,remaining}` | `total not_in ["$0","None","Unlimited","No Limit"]` |
| `lifetime_maximum.{met_amount,remaining}` | `lifetime_maximum.total not_in ["No Limit","Unlimited"]` |

The three `collected_per="call"` paths (always in the ask set, whatever is on file):
`patient_verification.is_insurance_active`, `insurance_representative.rep_name`,
`insurance_representative.call_reference_number`.

---

### Task 1: the scenario harness and the four global invariants

**Files:** create `tests/unit/forms/test_retry_scope_simulation.py`.

**Interfaces:** consumes `focus_paths`, `FieldStatus` (from `vera_core.forms.review`) and the
`conditions` primitives. Produces the `Scenario` builder and `assert_invariants`.

- [ ] **Step 1: build the scenario harness**

A `Scenario` describes one form's state going into a retry:

```python
@dataclass(frozen=True)
class Scenario:
    name: str
    values: dict[str, Any]            # what is on file
    confirmed_by_authoritative: bool  # did an AUTHORITATIVE call collect `values`?
```

with a helper turning it into the `(status_by_path, values, authoritative_calls)` triple
`focus_paths` needs. Two sources matter and must both be exercisable:

- `confirmed_by_authoritative=True` → every path in `values` gets
  `FieldStatus("ai_call", True, 95, AUTH)` and `authoritative_calls={AUTH}`;
- `confirmed_by_authoritative=False` → same rows but `call_id=OTHER`, so nothing is confirmed.

Also provide an `intake` variant (`FieldStatus("intake", None, None, None)`, `call_id=None`) — spec
D8's headline case.

- [ ] **Step 2: the four global invariants**

One function, called by EVERY scenario test. These are the guarantee the whole plan exists to give.

```python
def assert_invariants(doc, raw, scenario, focus: list[str]) -> None:
```

**I1 — SOUNDNESS: never ask a question that must not be asked.** This is the headline requirement.
For every path in `focus`, derived from `conditions.leaf_gates` + `is_applicable`:
  * its leaf role is in `COLLECTED_ROLES` (a call can only ask/confirm), and
  * its gate chain is satisfied by `scenario.values`.
The one sanctioned exception is the three `collected_per="call"` paths, which are unconditional by
design — assert them separately (I4) and exempt them here only if their own gates are satisfied
(they are ungated in this schema, so no exemption should actually be needed; if one IS needed, that
is a finding — report it).

**I2 — COMPLETENESS: never skip a question that must be asked.** Every path that is required,
applicable and collectable under `scenario.values` and was NOT confirmed by an authoritative call
must be in `focus`. Derive the required∧applicable∧collectable set from `leaf_gates`/`is_applicable`/
`is_required`; treat "confirmed" as `scenario.confirmed_by_authoritative and path in values`.
Note `include_defaulted=True` is the retry rule, so a leaf with a `default` is still owed —
do NOT exclude defaulted leaves here.

**I3 — GROUP CLOSURE: a partly-owed group is asked whole.** For every group in `doc.group_paths()`,
if any collectable leaf under it is in `focus`, then ALL of that group's collectable leaves that are
applicable under `scenario.values` are in `focus`. This is the behaviour the user asked to be pinned:
one missing answer in a group pulls the whole group.

**I4 — CALL-SCOPED ALWAYS:** `doc.collected_per_call_paths() <= set(focus)`, in every scenario,
including the one where an authoritative call confirmed literally everything.

- [ ] **Step 3: prove the invariants discriminate**

An invariant that cannot fail is worse than none. For each of I1–I4, temporarily break
`focus_paths` in the way that invariant is meant to catch, confirm the invariant FAILS, then restore
`review.py` exactly and confirm `git diff` on it is empty. Suggested mutations:

| invariant | mutation that must trip it |
| --- | --- |
| I1 | drop the `is_applicable` filter inside `_required_paths` |
| I2 | swap `include_defaulted=True` → `False` in `focus_paths` |
| I3 | drop the `expand_to_groups(...)` call, using `owed` directly |
| I4 | drop the `| doc.collected_per_call_paths()` union |

Record each result in the report. If any mutation does NOT trip its invariant, the invariant is too
weak — strengthen it before moving on.

- [ ] **Step 4: commit**

```bash
git add tests/unit/forms/test_retry_scope_simulation.py
git commit -m "test(retry): scenario harness and the four retry-scope invariants"
```

---

### Task 2: the conditional-gating scenarios

**Files:** extend `tests/unit/forms/test_retry_scope_simulation.py`.

Every scenario below runs `assert_invariants` AND its own scoped exact-set assertion. Scoped means
"the focus set restricted to this section or group equals exactly this literal set" — small enough to
write honestly, specific enough to be a real regression pin. Use `==`, not `<=`.

- [ ] **Step 1: coverage-type gating (spouse details)**

1. `coverage_type="Individual"` → `focus ∩ patient_information.* == set()` for the spouse leaves.
   Both `spouse_partner_name` and `spouse_partner_dob` must be ABSENT: they are
   `required when family_coverage`, so on an individual plan they are neither required nor
   applicable.
2. `coverage_type="Family"`, spouse leaves unanswered → both PRESENT. They are `role="confirm"` with
   `default="N/A"`, and the retry ask set includes defaulted leaves, so a default must NOT excuse
   them.
3. `coverage_type` **unanswered** → both ABSENT, because `family_coverage` is unsatisfied. State in
   the test that this is the gate-parent case: recovering these two belongs to
   `focus_questions(explode=True)` in the compiled plan, not to the path set.

- [ ] **Step 2: plan-type gating (PCP referral)**

1. `plan_type="PPO"` → `pcp_referral_required` ABSENT.
2. `plan_type="HMO"`, unanswered → PRESENT.
3. `plan_type="HMO"` and the leaf confirmed by an authoritative call → ABSENT.
4. `plan_type="HMO"` and the leaf confirmed by a NON-authoritative call → PRESENT (spec D8).

- [ ] **Step 3: two-level gating (male partner)**

`male_partner_in_scope` is `family_coverage AND spouse_gender == "Male"` — assert each level:

1. `coverage_type="Individual"`, `spouse_gender="Male"` → the whole
   `sections.male_partner_coverage.*` subtree ABSENT.
2. `coverage_type="Family"`, `spouse_gender="Female"` → whole subtree ABSENT.
3. `coverage_type="Family"`, `spouse_gender="Male"`, `male_partner_covered` unanswered →
   `focus ∩ male_partner_coverage.* == {male_partner_covered}` EXACTLY. The panels behind it must
   not appear yet.
4. …plus `male_partner_covered="Yes"` (authoritatively confirmed) → the `semen_analysis` and
   `sperm_cryopreservation` groups appear in full, and `male_partner_covered` itself does not.

- [ ] **Step 4: existence-flag gating (TPA, PBM, ISP, enrollment)**

Parametrize over the four identically-shaped cases:

| flag | detail leaves |
| --- | --- |
| `third_party_administrator.tpa_exists` | `tpa_name`, `tpa_phone` |
| `pharmacy_benefit_manager.pbm_exists` | `pbm_name`, `pbm_phone` |
| `infertility_specialty_pharmacy.isp_exists` | `isp_name`, `isp_phone` |
| `enrollment.enrollment_required` | `enrollment_provider_name`, `enrollment_provider_phone` |

For each: flag unanswered → `focus ∩ section == {flag}`; flag `="No"` authoritatively confirmed →
`focus ∩ section == set()`; flag `="Yes"` authoritatively confirmed → `focus ∩ section ==
{the two detail leaves}`.

Note `enrollment_required` carries `default="N/A"` and `center_of_excellence_required` sits in the
same section ungated — account for it in the expected sets rather than letting it surprise you.

- [ ] **Step 5: the 27-way prior-auth rollup**

`authorization_department.auth_department_{name,phone}` are gated on
`any_service_requires_prior_auth`, an `any` over 27 different `.prior_auth == "Yes"` fields.

1. No service has `prior_auth="Yes"` → both ABSENT.
2. Exactly ONE service has `prior_auth="Yes"` (pick `diagnostic_testing.labs_xray_ultrasound.
   cpt_58340.prior_auth`, and set the values its own gate chain needs) → both PRESENT.
3. A DIFFERENT single service (`male_partner_coverage.semen_analysis.cpt_89320.prior_auth`, with its
   deeper gate chain satisfied) → both PRESENT. Two different disjuncts, so the test proves the
   rollup is a real `any` and not one hardcoded field.

- [ ] **Step 6: money sentinel gating**

`met_amount`/`remaining` are gated `total not_in [...sentinels...]`. Parametrize over the three
triplets (`deductibles.individual`, `out_of_pocket.individual`, `lifetime_maximum`):

1. `total` = a sentinel (`"$0"`, `"Unlimited"`, `"No Limit"` — use each triplet's own list) →
   `met_amount` and `remaining` ABSENT.
2. `total` = a real amount, authoritatively confirmed → both PRESENT.

- [ ] **Step 7: commit**

```bash
git commit -m "test(retry): conditional-gating scenarios for the retry ask set"
```

---

### Task 3: group closure, either/or sets, and the two end-to-end extremes

**Files:** extend `tests/unit/forms/test_retry_scope_simulation.py`.

- [ ] **Step 1: group closure — the user's headline case**

Pick the 32-member `diagnostic_testing.labs_xray_ultrasound` panel and the 4-member
`cpt_58340` group inside it.

1. Authoritatively confirm EVERY applicable path except a single leaf
   (`labs_xray_ultrasound.cpt_58340.copay`) → assert `focus ∩ labs_xray_ultrasound.* ==` the full
   32-member collectable set. One missing answer pulls the entire panel.
2. Repeat with a different single missing leaf in a different group
   (`general_coverage.office_visits.cpt_99211.coinsurance`) → its whole group returns.
3. Assert the converse: with EVERY path in that panel authoritatively confirmed, the panel
   contributes NOTHING to `focus`.

- [ ] **Step 2: either/or sets**

17 alternatives exist; the copay-vs-coinsurance pairs are the interesting ones. For
`ovulation_induction.{copay,coinsurance}`:

1. `copay` authoritatively confirmed, `coinsurance` absent → `coinsurance` is NOT owed on its own
   account (one answer satisfies the pair). It may still appear via group closure — if it does,
   assert that explicitly and say WHY, so the test documents the interaction rather than hiding it.
2. Neither answered → both owed.

- [ ] **Step 3: the two extremes — exact whole-set assertions**

These two are the strongest assertions in the suite because the expected set is exact and tiny or
exact and total.

1. **Everything confirmed by an authoritative call** → `set(focus) ==
   doc.collected_per_call_paths()`, exactly three paths. Nothing else may survive.
2. **Everything answered but by a NON-authoritative call** → `focus` contains every
   required∧applicable∧collectable path (derive that set from the `conditions` primitives) plus the
   three call-scoped ones. Nothing is trusted, so nothing is skipped. This is spec D8's whole point.
3. **Everything supplied by INTAKE** → same as (2). An intake value was never put to the payer.

- [ ] **Step 4: full gate and report the measured shape**

Run `just check`. Then print, for the record, the focus-set size for each scenario in the suite as a
table (name → count). Paste it in the report — it is the readable summary of what a retry asks in
each real situation, and a future reader can diff it.

- [ ] **Step 5: commit**

```bash
git commit -m "test(retry): group closure, either/or, and whole-set extremes"
```

---

## Verification

Plan F is done when:

- `just check` passes verbatim.
- Every scenario asserts BOTH directions: `assert_invariants` (nothing spurious, nothing skipped,
  groups whole, call-scoped always) plus its own scoped exact-set assertion.
- All four invariants are shown to FAIL under their named mutation (Task 1 Step 3), with the tree
  restored and `git diff` clean afterwards.
- No file under `packages/vera_core/` is modified. A real defect found in `focus_paths` is REPORTED,
  not patched.
- The report carries the scenario → focus-count table.
