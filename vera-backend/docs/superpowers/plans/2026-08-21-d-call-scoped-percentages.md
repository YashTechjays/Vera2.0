# Plan D — both percentages count only what a call can fill

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `completion_pct` and `verified_pct` measure what a call can actually change, so the
retry-versus-park gates stop being biased by fields no call ever touches.

**Architecture:** Two denominators shrink to `ask`/`confirm` leaves, and `verified_pct`'s numerator
switches to `is_call_confirmed`. `unsatisfied_required_paths` is untouched. Stored values come right
via a re-seed, not a migration.

**Tech Stack:** Python 3.12, pydantic v2, pytest, ruff, mypy `--strict`; TypeScript + vitest for the
frontend mirror.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md` (D9)

## Global Constraints

- Every command runs from `vera-backend/`, except Task 3 which runs from `vera-frontend/`.
- **`unsatisfied_required_paths` is NOT modified.** It gates `READY_FOR_REVIEW`, where an
  intake-supplied patient name legitimately IS satisfied — a human is signing the form off, not
  verifying it against the payer. It keeps `is_field_satisfied` and `askable_only=False`.
- **`is_field_satisfied` is NOT modified.** Plan B added `is_call_confirmed` beside it.
- **Both percentage changes land together.** `post_call.py:93` compares `completion_pct` against
  `tenant.retry_fill_threshold` while `post_call_eval.py:511` compares `verified_fraction` against
  the *same* setting. Changing one denominator and not the other makes one threshold value mean two
  different things depending on which resolver ran.
- **`verified_pct`'s two edits also land together** — authoritative-only satisfaction against the
  current `askable_only=False` denominator keeps the never-collectable leaves in the divisor while
  making them permanently unsatisfiable, capping `verified_pct` at 90.9% on the seeded form and
  making any `retry_fill_threshold` above that unreachable.
- No migration. Stored `completion_pct` / `verified_pct` come right by re-seeding (spec D7).
- Never log a field value.
- Depends on Plan B for `is_call_confirmed` and `load_authoritative_call_ids`.
- `just check` verbatim, then `/simplify`, then `just check` again. Frontend: `tsc` + `eslint` +
  `vitest` + `build`.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/review.py`
  - `completion_pct_v2` — role filter (Task 1)
  - `satisfied_required_fraction` — `is_call_confirmed` + `askable_only=True` (Task 2)
- **Modify** `packages/vera_core/src/vera_core/services/post_call_eval.py:463` — pass the
  authoritative-call set (Task 2)
- **Modify** `scripts/seed_retry_form.py:370` — same (Task 2)
- **Modify** `vera-frontend/src/lib/ibv/schema.ts:316` — `completionPercent` role filter (Task 3)
- **Test** `tests/unit/forms/test_review.py` — both percentages
- **Test** `vera-frontend/src/lib/ibv/schema.test.ts` — the mirror

**Interfaces:**

- Consumes: `is_call_confirmed(status, *, authoritative_calls, floor)` and
  `load_authoritative_call_ids(session, form_id, *, reference_field)` from Plan B.
- Produces the **signature change**:

```python
def satisfied_required_fraction(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
    authoritative_calls: Collection[UUID],
) -> float: ...
```

---

### Task 1: `completion_pct` counts only collectable leaves

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py` (`completion_pct_v2`, line 149)
- Test: `tests/unit/forms/test_review.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `completion_pct_v2` with a role-filtered denominator.

`completion_pct_v2` has its own loop over `leaf_gates(doc)` — it does **not** go through
`_required_paths`, so Plan B's `include_defaulted` parameter is irrelevant here. The change is one
clause in that comprehension.

- [ ] **Step 1: Write the failing test**

```python
class TestCompletionCountsOnlyCollectableLeaves:
    """A call can only fill `ask`/`confirm` leaves, so the rest do not measure a call's progress.

    Worse than inert: every non-askable required leaf in `ibv_form_standard_v2` is also a
    `required_intake_fields` target, so `missing_required` blocks form creation without them and
    they are ALWAYS filled — a constant 30.6% (21.4% for disease_only) that no call can move. And
    `post_call.py:93` gates a retry on this number.
    """

    def test_a_form_with_only_intake_context_reads_zero(self) -> None:
        raw = _ibv_raw()
        doc = FormSchemaDoc.model_validate(raw)
        values = {
            path: "x"
            for path, leaf in doc.leaf_items()
            if leaf.role not in COLLECTED_ROLES
        }
        assert completion_pct_v2(values, raw) == 0.0

    def test_context_leaves_do_not_dilute_a_collected_answer(self) -> None:
        """One askable leaf filled out of N askable — not out of N + 15."""
        raw = _ibv_raw()
        doc = FormSchemaDoc.model_validate(raw)
        context = {p: "x" for p, leaf in doc.leaf_items() if leaf.role not in COLLECTED_ROLES}
        target = "sections.patient_verification.is_insurance_active"
        with_one = completion_pct_v2({**context, target: "Yes"}, raw)
        askable_denominator = len(
            [
                p
                for p, leaf, gates in leaf_gates(doc)
                if leaf.role in COLLECTED_ROLES
                and is_applicable(gates, {**context, target: "Yes"}, doc.shared_conditions or {})
                and is_required(leaf, {**context, target: "Yes"}, doc.shared_conditions or {})
            ]
        )
        assert with_one == round(1 / askable_denominator * 100, 2)

    def test_a_schema_with_no_askable_required_leaves_is_complete(self) -> None:
        """The `if not relevant: return 100.0` branch still holds when the filter empties it."""
        raw = _context_only_doc()      # a minimal doc whose only required leaf is role=context
        assert completion_pct_v2({}, raw) == 100.0
```

`_ibv_raw()` loads `data/form_schemas/ibv_form_standard_v2.json`; `_context_only_doc()` is a
`minimal_doc(...)` variant whose single required leaf has `role="context"` and no prompt. Read
`test_review.py`'s existing fixtures first and reuse them.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_review.py -k CompletionCountsOnly -v`
Expected: FAIL — the first test reports about `30.61`, not `0.0`. That number is the defect.

- [ ] **Step 3: Add the role filter**

```python
def completion_pct_v2(values: Mapping[str, Any], schema_json: Mapping[str, Any]) -> float:
    """DSL 2.x completion (0-100, 2 dp): required ∧ applicable ∧ COLLECTABLE leaves, evaluated
    against the current answer values (`applicable_when` chains from the section down,
    `required: bool | {when}`). A leaf with a declared `default` counts as filled — display/export
    assume it (spec §4.4). Mirrors the frontend's `completionPercent`.

    Restricted to `ask`/`confirm` because only those can be filled BY A CALL, and this number gates
    the `low_fill` retry decision in `post_call.py`. Every non-askable required leaf is also a
    `required_intake_fields` target, so it is always filled and contributed a constant 30.6% offset
    that no call could move — a brand-new form read as a third complete (spec D9).
    """
    doc = FormSchemaDoc.model_validate(schema_json)
    shared = doc.shared_conditions or {}
    relevant = [
        (path, leaf)
        for path, leaf, gates in leaf_gates(doc)
        if leaf.role in COLLECTED_ROLES
        and is_applicable(gates, values, shared)
        and is_required(leaf, values, shared)
    ]
    if not relevant:
        return 100.0
    alternatives = alternative_index(alternative_pairs(doc))
    filled = sum(
        1 for path, leaf in relevant if is_satisfied(path, leaf.default, values, alternatives)
    )
    return round(filled / len(relevant) * 100, 2)
```

`COLLECTED_ROLES` is already imported in `review.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/ -v`
Expected: PASS. Existing `completion_pct_v2` tests that assert a specific percentage **will move** —
read each one before editing it and confirm the new number is right by hand, rather than pasting
whatever the failure reports. Any test whose fixture is context-only now expects `100.0` (empty
denominator), which is a semantic change worth a comment in that test.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py tests/unit/forms/test_review.py
git commit -m "fix(forms): completion_pct counts only leaves a call can fill"
```

---

### Task 2: `verified_pct` counts only what an authoritative call confirmed

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py` (`satisfied_required_fraction`)
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py:463`
- Modify: `scripts/seed_retry_form.py:370`
- Test: `tests/unit/forms/test_review.py`

**Interfaces:**
- Consumes: `is_call_confirmed` (Plan B), `load_authoritative_call_ids` (Plan B).
- Produces: `satisfied_required_fraction` with the `authoritative_calls` keyword.

- [ ] **Step 1: Write the failing test**

```python
class TestVerifiedCountsOnlyAuthoritativeAnswers:
    def test_intake_values_are_not_verified(self) -> None:
        """The headline defect: a form nobody has called reported 100% verified and routed to
        READY_FOR_REVIEW — "nothing is wrong, sign it off" — with zero judge verdicts in existence.
        """
        raw = _ibv_raw()
        doc = FormSchemaDoc.model_validate(raw)
        status, values = {}, {}
        for path, leaf, gates in leaf_gates(doc):
            if is_applicable(gates, values, doc.shared_conditions or {}):
                status[path] = FieldStatus("intake", None, None, None)
                values[path] = "x"
        assert satisfied_required_fraction(
            status, raw, floor=70, values=values, authoritative_calls=set()
        ) == 0.0

    def test_an_answer_from_a_non_authoritative_call_is_not_verified(self) -> None:
        raw, auth, other = _ibv_raw(), uuid4(), uuid4()
        target = "sections.patient_verification.is_insurance_active"
        status = {target: FieldStatus("ai_call", True, 95, other)}
        frac = satisfied_required_fraction(
            status, raw, floor=70, values={target: "Yes"}, authoritative_calls={auth}
        )
        assert frac == 0.0

    def test_the_same_answer_from_an_authoritative_call_is(self) -> None:
        raw, auth = _ibv_raw(), uuid4()
        target = "sections.patient_verification.is_insurance_active"
        status = {target: FieldStatus("ai_call", True, 95, auth)}
        frac = satisfied_required_fraction(
            status, raw, floor=70, values={target: "Yes"}, authoritative_calls={auth}
        )
        assert frac > 0.0

    def test_one_hundred_percent_stays_reachable(self) -> None:
        """The reason the denominator must shrink too: with `askable_only=False` the 15 never-
        collectable leaves stay in the divisor while becoming permanently unsatisfiable, capping
        this at 90.9% — so a retry_fill_threshold above that would never fire the park gate."""
        raw, auth = _ibv_raw(), uuid4()
        doc = FormSchemaDoc.model_validate(raw)
        status, values = {}, {}
        for path, leaf, gates in leaf_gates(doc):
            if leaf.role in COLLECTED_ROLES:
                status[path] = FieldStatus("ai_call", True, 95, auth)
                values[path] = "Yes"
        assert satisfied_required_fraction(
            status, raw, floor=70, values=values, authoritative_calls={auth}
        ) == 1.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_review.py -k VerifiedCountsOnly -v`
Expected: FAIL — `satisfied_required_fraction()` got an unexpected keyword argument
`authoritative_calls`.

- [ ] **Step 3: Change the function**

```python
def satisfied_required_fraction(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
    authoritative_calls: Collection[UUID],
) -> float:
    """Fraction (0.0-1.0) of required, applicable, COLLECTABLE leaves an AUTHORITATIVE call
    confirmed — what `verified_pct` reports and what the park gate compares against
    `tenant.retry_fill_threshold`.

    Two restrictions, and they only work together (spec D9):

    * `is_call_confirmed`, not `is_field_satisfied` — an intake value is trusted for completeness
      and for the retry-worthiness decision but was never put to the payer, and an answer from a
      call that captured no reference number is not proof either;
    * `askable_only=True` — the never-collectable leaves would otherwise stay in the denominator
      while becoming permanently unsatisfiable, capping this at 90.9% on the IBV form.

    NOT the auto-complete gate: `unsatisfied_required_paths` keeps `is_field_satisfied` and all
    roles, because a human signing a form off legitimately trusts an intake-supplied patient name.
    """
    gate_values = _gate_values(status_by_path, values)
    applicable = _required_paths(schema_json, gate_values, askable_only=True)
    if not applicable:
        return 1.0
    alternatives = _alternatives(schema_json)
    confirmed = sum(
        1
        for path in applicable
        if _confirmed(path, status_by_path, alternatives, authoritative_calls, floor=floor)
    )
    return confirmed / len(applicable)
```

`_confirmed` is the either/or-aware helper Plan B added beside `_satisfied`. This function no longer
routes through `_unsatisfied`, so its comment about "the one set all three public consumers measure"
needs updating there — `unsatisfied_required_paths` and `retryable_required_paths` still share
`_unsatisfied`; this one no longer does, and the docstring above says why.

- [ ] **Step 4: Update the two call sites**

`post_call_eval.py`, around line 462:

```python
    status_by_path = await load_field_status(session, form_id)
    authoritative = await load_authoritative_call_ids(
        session, form_id, reference_field=doc.rep_call_reference_number_field
    )
    verified_fraction = satisfied_required_fraction(
        status_by_path,
        version.schema_json,
        floor=deps.floor,
        values=current_values,
        authoritative_calls=authoritative,
    )
```

`doc` may not be in scope at that point — the module parses the schema earlier for the
`UNSUPPORTED_SCHEMA` branch; reuse that parsed document rather than re-validating.

`scripts/seed_retry_form.py:370` needs the same keyword. The seeded call captures a reference number,
so pass its id and the printed `verified` figure stays close to today's.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/ tests/integration/test_post_call_eval.py -v`
Expected: PASS. `test_post_call_eval.py` has cases asserting a specific `verified_pct` or a
`FILL_THRESHOLD_MET` outcome — each needs re-reading against the new definition. A case whose answers
came from a call with no reference-number answer now verifies **0%**; that is the fix, not a broken
test, but confirm each one by hand.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py \
        packages/vera_core/src/vera_core/services/post_call_eval.py \
        scripts/seed_retry_form.py \
        tests/unit/forms/test_review.py
git commit -m "fix(forms): verified_pct counts only authoritatively confirmed answers"
```

---

### Task 3: the frontend mirror

**Files:**
- Modify: `vera-frontend/src/lib/ibv/schema.ts:316` (`completionPercent`)
- Test: `vera-frontend/src/lib/ibv/schema.test.ts`

**Interfaces:**
- Consumes: nothing from the backend at runtime — this is an independent reimplementation that must
  agree with `completion_pct_v2`.
- Produces: a `completionPercent` that matches the backend.

Run everything in this task from `vera-frontend/`.

- [ ] **Step 1: Write the failing test**

```ts
describe("completionPercent counts only leaves a call can fill", () => {
  it("reads 0 for a form holding only intake context", () => {
    const values = Object.fromEntries(
      allLeaves(schema)
        .filter((l) => l.field.role !== "ask" && l.field.role !== "confirm")
        .map((l) => [l.path, "x"])
    )
    expect(completionPercent(schema, values)).toBe(0)
  })

  it("agrees with the backend on the seeded fixture", () => {
    // The backend's completion_pct_v2 is the authority; this mirror must not drift from it.
    expect(completionPercent(schema, SEEDED_VALUES)).toBe(SEEDED_BACKEND_PCT)
  })
})
```

`SEEDED_VALUES` / `SEEDED_BACKEND_PCT`: take them from the backend by running
`just seed-retry-form` and reading the printed `completion` figure, then pin both here. Check
whether this test module already has a schema fixture and a drift test — reuse them.

- [ ] **Step 2: Run it to verify it fails**

Run: `npm test -- schema.test.ts`
Expected: FAIL — the first case reports ~31, not 0.

- [ ] **Step 3: Add the role filter**

`schema.ts:316`:

```ts
/**
 * 0–100 completion over required ∧ applicable ∧ COLLECTABLE leaves (defaults count filled).
 * Mirrors the backend's `completion_pct_v2`, which is the authority — only `ask`/`confirm` leaves
 * can be filled by a call, and every other required leaf is an intake target that is always
 * present, so counting them added a constant offset no call could move.
 */
export function completionPercent(schema: FormSchema, values: FormValues): number {
  const relevant = allLeaves(schema).filter(
    (l) =>
      (l.field.role === "ask" || l.field.role === "confirm") &&
      isApplicable(schema, l.gates, values) &&
      isRequired(schema, l.field, values)
  )
  if (relevant.length === 0) return 100
  const filled = relevant.filter((l) => isSatisfied(schema, l, values)).length
  return Math.round((filled / relevant.length) * 100)
}
```

If this module already exports a collected-roles predicate (`voiceDisposition` distinguishes
`asked`), reuse it rather than inlining the two role literals.

- [ ] **Step 4: Run the frontend gate**

Run: `npx tsc -b && npx eslint . && npm test && npm run build`
Expected: PASS. Other tests asserting a completion percentage will move — verify each new number
against the backend rather than against the failure output.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/ibv/schema.ts vera-frontend/src/lib/ibv/schema.test.ts
git commit -m "fix(ibv): completionPercent counts only leaves a call can fill"
```

---

### Task 4: bring stored values right, and re-examine the threshold

**Files:** none — this is the data step and a decision to surface.

- [ ] **Step 1: Run the full gate**

Run: `just check`, then `/simplify`, then `just check`.
Expected: PASS on the exact tree to be pushed.

- [ ] **Step 2: Re-seed, so stored values are recomputed**

Stored `completion_pct` and `verified_pct` were written under the old definitions and no migration
brings them forward (spec D7). Re-seeding rewrites both — `recompute_form_projection` for completion,
the seed script's own call for verified.

```bash
just test_seed_patient_data
just seed-retry-form
```
Expected: `seed-retry-form` prints a `completion / verified` pair different from before this plan.
Record both numbers in the PR body.

- [ ] **Step 3: Confirm no form still carries a stale percentage**

```bash
DB=$(uv run python -c "
from sqlalchemy.engine import make_url
from vera_core.config import get_settings
print(make_url(get_settings().database_url).database)")
docker exec vera-backend-postgres-1 psql -U vera -d "$DB" -c "
select chart_number, status, completion_pct, verified_pct, updated_at
from patient_form order by updated_at"
```
Expected: every row's `updated_at` is from this re-seed. A row older than it is carrying a percentage
computed under the old definition — re-seed or delete it.

- [ ] **Step 4: Surface the threshold decision**

`tenant.retry_fill_threshold` defaults to 0.5 and is compared against both numbers. Both now measure
*call-fillable* fields rather than all required ones, so 0.5 means something different — and
something consistent for the first time, since `post_call.py` and `post_call_eval.py` finally measure
the same population.

This is a product decision, not a code change. Put the before/after in the PR body — the seeded
form's old and new `completion` / `verified` pair, and the observation that an IBV form used to start
at 30.6% complete and now starts at 0% — and ask whoever owns tenant configuration whether 0.5 is
still the number they want.

Do **not** change the default as part of this plan. Changing the measurement and the threshold in
one commit makes the behaviour change impossible to attribute.

- [ ] **Step 5: Commit**

```bash
git commit --allow-empty -m "docs(forms): record the retry_fill_threshold question"
```

Only if `/simplify` produced no edits; otherwise stage those and drop the flag.

---

## Verification

Plan D is done when:

- `just check` passes verbatim on the pushed tree, and the frontend gate (`tsc` + `eslint` +
  `vitest` + `build`) passes from `vera-frontend/`.
- A form holding only intake context reports **0%** complete and **0%** verified — it used to report
  30.6% and 100%.
- `verified_pct` can still reach 100% (Task 2 Step 1's last test) — proof the denominator shrank
  alongside the numerator.
- `unsatisfied_required_paths` is untouched: `git diff` shows no change to it, and the
  `READY_FOR_REVIEW` cases in `test_post_call_eval.py` still pass unedited.
- The frontend mirror agrees with the backend on the seeded fixture.
- Every `patient_form` row's percentages were rewritten by the re-seed (Task 4 Step 3).
- The PR body carries the before/after numbers and the open `retry_fill_threshold` question.

No live call is needed: this plan changes measurement, not spoken behaviour. It does change *whether*
a retry is dispatched, so the live verification that matters is Plan C's — run after this lands, a
form that should retry must actually get dispatched rather than parked as `FILL_THRESHOLD_MET`.
