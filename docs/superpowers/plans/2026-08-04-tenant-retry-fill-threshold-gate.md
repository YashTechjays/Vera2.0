# Tenant Retry Fill-Threshold Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the post-call auto-retry decision honor the per-tenant `retry_fill_threshold`: if a call verified ≥ threshold of a form's applicable-required fields, route to human review instead of redialing the payer.

**Architecture:** Add a pure satisfaction-fraction helper in `forms/review.py`, a new `ReviewReason`, and one suppressor gate in `evaluate_call` (`post_call_eval.py`). Reuse the existing per-tenant `retry_fill_threshold` column, platform config endpoint, audit, validation, and `TenantFormDialog` UI (all already built) — only a UI copy tweak is needed there.

**Tech Stack:** Python 3.12 (uv workspace, SQLAlchemy async, pytest-asyncio), React + Vite + TypeScript (vitest).

## Global Constraints

- Backend gate: `just check` (ruff check + ruff format --check + mypy --strict + pytest) green before done.
- Frontend gate: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` green.
- PEP 695 type params only; no `TypeVar`/`Generic`.
- Comparison boundary is **`>=`** (a form exactly at threshold does not retry).
- The gate only **suppresses** a retry; it never forces one and never overrides the askable / budget / user-ended / auto-retry guards.
- `tenant.retry_fill_threshold` is `Numeric` → `Decimal`; cast with `float(...)` before comparison.
- No PHI in logs; the new reason carries counts/paths only (paths are schema constants).
- Do NOT modify the fallback resolver `post_call.py` (out of scope) or the config endpoint/definer-fn/validation (already exist).

---

### Task 1: `satisfied_required_fraction` helper

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/review.py` (add after `retryable_required_paths`, ~L300)
- Test: `vera-backend/tests/unit/forms/test_review.py`

**Interfaces:**
- Consumes: existing `_required_paths`, `_gate_values`, `is_field_satisfied`, `FieldStatus` (same module).
- Produces: `satisfied_required_fraction(status_by_path, schema_json, *, floor: int, values: Mapping[str, Any] | None = None) -> float`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/forms/test_review.py` (uses the existing module-level `SCHEMA` — 3 required paths — and the existing `_status()` helper):

```python
class TestSatisfiedRequiredFraction:
    def test_none_satisfied_is_zero(self) -> None:
        assert satisfied_required_fraction({}, SCHEMA, floor=70) == 0.0

    def test_one_of_three_satisfied(self) -> None:
        status = {"patient_information.patient_name": _status()}
        assert satisfied_required_fraction(status, SCHEMA, floor=70) == 1 / 3

    def test_all_satisfied_is_one(self) -> None:
        status = {
            "patient_information.patient_name": _status(),
            "patient_information.patient_dob": _status(),
            "insurance_information.policy_number": _status(),
        }
        assert satisfied_required_fraction(status, SCHEMA, floor=70) == 1.0

    def test_no_applicable_required_is_one(self) -> None:
        assert satisfied_required_fraction({}, {"sections": []}, floor=70) == 1.0

    def test_ai_answer_below_floor_counts_unsatisfied(self) -> None:
        # ties to the judge-coverage fix: verified means supported AND confident
        weak = FieldStatus(source="ai_call", ai_supported=True, ai_confidence=50)
        status = {"patient_information.patient_name": weak}
        assert satisfied_required_fraction(status, SCHEMA, floor=70) == 0.0

    def test_consistent_with_unsatisfied_required_paths(self) -> None:
        status = {"patient_information.patient_name": _status()}
        applicable = 3  # SCHEMA has 3 required paths
        unsat = len(unsatisfied_required_paths(status, SCHEMA, floor=70))
        assert satisfied_required_fraction(status, SCHEMA, floor=70) == (
            (applicable - unsat) / applicable
        )
```

Ensure `satisfied_required_fraction` and `unsatisfied_required_paths` are in the `from vera_core.forms.review import (...)` block at the top of the test file.

- [ ] **Step 2: Run the tests — verify they fail**

Run: `uv run pytest tests/unit/forms/test_review.py::TestSatisfiedRequiredFraction -v`
Expected: FAIL with `ImportError`/`AttributeError` (helper not defined).

- [ ] **Step 3: Implement the helper**

In `forms/review.py`, immediately after `retryable_required_paths`:

```python
def satisfied_required_fraction(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
) -> float:
    """Fraction (0.0–1.0) of required, applicable fields that are satisfied — the
    same set unsatisfied_required_paths measures. A form with no applicable-required
    fields is complete → 1.0. Satisfaction uses is_field_satisfied (verified: AI
    answers need supported + confidence >= floor), not mere value presence."""
    gate_values = _gate_values(status_by_path, values)
    applicable = _required_paths(schema_json, gate_values, askable_only=False)
    if not applicable:
        return 1.0
    satisfied = sum(
        1 for path in applicable if is_field_satisfied(status_by_path.get(path), floor=floor)
    )
    return satisfied / len(applicable)
```

- [ ] **Step 4: Run the tests — verify they pass**

Run: `uv run pytest tests/unit/forms/test_review.py::TestSatisfiedRequiredFraction -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/forms/review.py vera-backend/tests/unit/forms/test_review.py
git commit -m "feat(review): satisfied_required_fraction helper for threshold-gated retry"
```

---

### Task 2: New review reason + suppressor gate in `evaluate_call`

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/enums.py` (add to `ReviewReason`)
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/post_call_eval.py` (import helper; insert gate after the `not unsatisfied` block, ~L487; enum already imported)
- Test: `vera-backend/tests/integration/test_post_call_eval.py`

**Interfaces:**
- Consumes: `satisfied_required_fraction` (Task 1); `tenant.retry_fill_threshold` (existing column); `ReviewReason.FILL_THRESHOLD_MET` (this task).
- Produces: `evaluate_call` returns `EXCEPTION_REVIEW` with reason `fill_threshold_met` when `satisfied_required_fraction(...) >= float(tenant.retry_fill_threshold)`.

- [ ] **Step 1: Add the enum value**

In `models/enums.py`, inside `ReviewReason`, after `AUTO_RETRY_DISABLED`:

```python
    # Required fields remain unsatisfied and retryable, but the tenant's
    # retry_fill_threshold of applicable-required fields is already satisfied —
    # the call verified "enough", so the form parks for a human instead of
    # redialing the payer for the diminishing tail.
    FILL_THRESHOLD_MET = "fill_threshold_met"
```

- [ ] **Step 2: Write the failing integration tests**

In `tests/integration/test_post_call_eval.py`, model on the existing fixtures
(`seeded_ai_processing_form: _SeedCtx`, `fake_audit`, `fake_livekit`,
`FakeLLMClient`, `EvalDeps`, `evaluate_call`, and the existing helpers that seed
answers). Two cases, driven by the tenant's `retry_fill_threshold` and how many
required fields the fake verdicts satisfy:

```python
async def test_threshold_met_routes_to_review_not_retry(
    seeded_ai_processing_form, fake_audit, fake_livekit
) -> None:
    """At/above retry_fill_threshold → EXCEPTION_REVIEW / fill_threshold_met, no retry."""
    ctx = seeded_ai_processing_form
    # Arrange: threshold low enough that the satisfied answers clear it, but at
    # least one required field remains unsatisfied+askable (so, pre-change, it
    # would have retried). Set the tenant threshold and seed answers/verdicts per
    # the file's existing helpers so satisfied_fraction >= threshold.
    # ... (set ctx tenant.retry_fill_threshold; seed a satisfied required answer;
    #     leave one required askable field unsatisfied)
    deps = EvalDeps(llm=..., audit=fake_audit, livekit=fake_livekit, auto_retry_enabled=True)

    out = await evaluate_call(ctx.session, deps, tenant_id=ctx.tenant_id,
                              form_id=ctx.form_id, call_id=ctx.call_id, turns=[...])

    assert out.status == FormStatus.EXCEPTION_REVIEW
    reason = _latest_status_change_reason(fake_audit)  # existing audit-reading helper
    assert reason == ReviewReason.FILL_THRESHOLD_MET.value


async def test_below_threshold_still_retries(
    seeded_ai_processing_form, fake_audit, fake_livekit
) -> None:
    """Below threshold with an askable field + budget + auto-retry on → IN_QUEUE."""
    ctx = seeded_ai_processing_form
    # Arrange: threshold high (e.g. 1.0) so satisfied_fraction < threshold, with a
    # retryable unsatisfied field and auto_retry_enabled True.
    deps = EvalDeps(llm=..., audit=fake_audit, livekit=fake_livekit, auto_retry_enabled=True)

    out = await evaluate_call(ctx.session, deps, tenant_id=ctx.tenant_id,
                              form_id=ctx.form_id, call_id=ctx.call_id, turns=[...])

    assert out.status == FormStatus.IN_QUEUE
```

Fill the `...`/comments using the file's established seeding helpers (same ones
`test_verdict_path_mismatch_is_logged_not_silent` uses). If the file lacks an
audit-reason reader, assert on `out.status` plus the persisted `form.review_reason`
after the call instead.

- [ ] **Step 3: Run the tests — verify they fail**

Run: `uv run pytest tests/integration/test_post_call_eval.py -k threshold -v`
Expected: FAIL — `test_threshold_met_...` currently returns `IN_QUEUE` (retry) instead of review, and `FILL_THRESHOLD_MET` is asserted but not yet used.
(Integration tests need `just up` + `just migrate`; if the local DB is wedged, note it and rely on CI, which runs a fresh Postgres.)

- [ ] **Step 4: Implement the gate**

In `post_call_eval.py`, add the import near the other `forms.review` imports:

```python
from vera_core.forms.review import (
    REVIEW_CONFIDENCE_FLOOR,
    form_completion_pct,
    is_blank_answer,
    retryable_required_paths,
    satisfied_required_fraction,
    unsatisfied_required_paths,
    unwrap_value,
)
```

Then insert the gate immediately after the `if not unsatisfied:` block (~L487),
before `retryable = retryable_required_paths(...)`:

```python
    # Good-enough gate: if the call already verified the tenant's threshold of the
    # applicable-required fields, park for human review instead of redialing the
    # payer for the diminishing tail. Only suppresses a retry — the guards below
    # still decide the sub-threshold case.
    if satisfied_required_fraction(
        status_by_path, version.schema_json, floor=deps.floor, values=current_values
    ) >= float(tenant.retry_fill_threshold):
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=unsatisfied,
            reason=ReviewReason.FILL_THRESHOLD_MET,
        )
```

- [ ] **Step 5: Run the tests — verify they pass**

Run: `uv run pytest tests/integration/test_post_call_eval.py -k threshold -v`
Expected: PASS.

- [ ] **Step 6: Run the full backend gate**

Run: `just check`
Expected: ruff + mypy + pytest all green. Fix `test_enums.py` if it snapshots the `ReviewReason` members (add `fill_threshold_met`).

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/enums.py \
        vera-backend/packages/vera_core/src/vera_core/services/post_call_eval.py \
        vera-backend/tests/integration/test_post_call_eval.py
git commit -m "feat(post-call-eval): threshold-gated retry via tenant retry_fill_threshold"
```

---

### Task 3: Settings UI copy tweak

**Files:**
- Modify: `vera-frontend/src/components/platform/TenantFormDialog.tsx:51`
- Test: `vera-frontend/src/components/platform/TenantFormDialog.test.tsx` (if it asserts the label)

**Interfaces:**
- Consumes: existing `NUMBER_FIELDS` array and `retry_fill_threshold` form value (0–1). No API/type changes.
- Produces: clearer label; behavior unchanged.

- [ ] **Step 1: Update the label (and add an upper bound)**

In `TenantFormDialog.tsx`, change the `retry_fill_threshold` entry in `NUMBER_FIELDS`:

```tsx
  {
    key: "retry_fill_threshold",
    label: "Min verified fraction before review (0–1)",
    min: 0,
    max: 1,
    step: 0.05,
  },
```

If `NUMBER_FIELDS` entries are rendered without a `max`, thread `max` through the
input the same way `min`/`step` are already passed (add `max={f.max}` where the
`<input>` is built; keep it optional so other fields without `max` still render).

- [ ] **Step 2: Update/verify the test**

If `TenantFormDialog.test.tsx` asserts the old label text ("Retry fill threshold"),
update the assertion to the new label. If it does not reference the label, add a
minimal render test asserting the new label is present.

Run: `npx vitest run src/components/platform/TenantFormDialog.test.tsx`
Expected: PASS.

- [ ] **Step 3: Run the frontend gate**

Run: `npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/components/platform/TenantFormDialog.tsx \
        vera-frontend/src/components/platform/TenantFormDialog.test.tsx
git commit -m "feat(platform-ui): reword retry threshold as min verified fraction before review"
```

---

## Self-review notes

- **Spec coverage:** metric helper (Task 1), decision gate + reason + observability (Task 2), UI copy (Task 3), reuse of existing config plumbing (no task needed). Edge cases (zero applicable → 1.0; `>=` boundary; endpoints 0/1) covered by Task 1 tests + the gate. Out-of-scope items (fallback unification, critical-field override, tenant self-serve) intentionally excluded.
- **Type consistency:** helper signature matches `unsatisfied_required_paths`/`retryable_required_paths` (`status_by_path, schema_json, *, floor, values=None`); `float(tenant.retry_fill_threshold)` for the Decimal column.
- **No placeholders:** the only `...` are in the Task-2 integration test where exact seeding must follow the target file's existing fixtures; every production edit is concrete.
