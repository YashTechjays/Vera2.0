# Tenant Retry Fill-Threshold Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the post-call auto-retry decision honor the per-tenant `retry_fill_threshold` against a **verified** (satisfaction) fraction, and surface that "Verified %" next to the existing "Filled %" in the Live Monitoring Overview modal.

**Architecture:** Pure satisfaction-fraction helper in `forms/review.py`; a persisted `patient_form.verified_pct` (mirroring `completion_pct`) set in `evaluate_call`; a new `ReviewReason` + one suppressor gate in `evaluate_call`; `verified_pct` exposed on `CallSummary` and rendered in `LiveCallModal`. Reuse the existing per-tenant `retry_fill_threshold` column/endpoint/audit/validation and its `TenantFormDialog` control (copy tweak only).

**Tech Stack:** Python 3.12 (uv workspace, SQLAlchemy async, Alembic, pytest-asyncio), React + Vite + TypeScript (vitest).

## Global Constraints

- Backend gate: `just check` (ruff check + ruff format --check + mypy --strict + pytest) green before done. Frontend gate: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` green.
- PEP 695 type params only.
- Retry comparison boundary is **`>=`**; the gate only **suppresses** a retry, never forces one, never overrides the askable/budget/user-ended/auto-retry guards.
- `tenant.retry_fill_threshold` is `Numeric` → `Decimal`; cast `float(...)` before comparison. `verified_pct` is `Numeric(5,2)`, stored as `satisfied_fraction × 100` rounded 2dp.
- **Migrations must be idempotent** (`ADD COLUMN IF NOT EXISTS`) — CI runs `alembic upgrade head` from `0001` on a fresh Postgres whose `create_all` already made the column; an unguarded `ADD COLUMN` fails there. Random-hex revision id (via `just makemigration`), never hand-numbered.
- Verified % is **post-call only** (no evals mid-call) → reads 0 live; no SSE frame change.
- Do NOT touch the config endpoint/definer-fn/validation, the fallback resolver `post_call.py`, or the live SSE frames.

---

### Task 1: `satisfied_required_fraction` helper

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/review.py` (after `retryable_required_paths`, ~L300)
- Test: `vera-backend/tests/unit/forms/test_review.py`

**Interfaces:**
- Consumes: `_required_paths`, `_gate_values`, `is_field_satisfied`, `FieldStatus` (same module).
- Produces: `satisfied_required_fraction(status_by_path, schema_json, *, floor: int, values: Mapping[str, Any] | None = None) -> float`

- [ ] **Step 1: Write the failing tests** (uses the file's existing module-level `SCHEMA` — 3 required paths — and the `_status()` helper):

```python
class TestSatisfiedRequiredFraction:
    def test_none_satisfied_is_zero(self) -> None:
        assert satisfied_required_fraction({}, SCHEMA, floor=70) == 0.0

    def test_one_of_three_satisfied(self) -> None:
        assert satisfied_required_fraction(
            {"patient_information.patient_name": _status()}, SCHEMA, floor=70
        ) == 1 / 3

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
        weak = FieldStatus(source="ai_call", ai_supported=True, ai_confidence=50)
        assert satisfied_required_fraction(
            {"patient_information.patient_name": weak}, SCHEMA, floor=70
        ) == 0.0
```

Add `satisfied_required_fraction` to the `from vera_core.forms.review import (...)` block.

- [ ] **Step 2: Run — verify fail.** `uv run pytest tests/unit/forms/test_review.py::TestSatisfiedRequiredFraction -v` → FAIL (ImportError).

- [ ] **Step 3: Implement** in `forms/review.py` after `retryable_required_paths`:

```python
def satisfied_required_fraction(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
) -> float:
    """Fraction (0.0–1.0) of required, applicable fields that are satisfied — the
    same set unsatisfied_required_paths measures. No applicable-required fields → 1.0.
    Satisfaction is verified (is_field_satisfied: AI answers need supported +
    confidence >= floor), not mere value presence."""
    gate_values = _gate_values(status_by_path, values)
    applicable = _required_paths(schema_json, gate_values, askable_only=False)
    if not applicable:
        return 1.0
    satisfied = sum(
        1 for path in applicable if is_field_satisfied(status_by_path.get(path), floor=floor)
    )
    return satisfied / len(applicable)
```

- [ ] **Step 4: Run — verify pass.** Same command → PASS (5 tests).

- [ ] **Step 5: Commit.** `feat(review): satisfied_required_fraction helper for threshold-gated retry`

---

### Task 2: `verified_pct` column + migration

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/patient_form.py` (beside `completion_pct`, L83)
- Create: `vera-backend/migrations/versions/<hex>_add_patient_form_verified_pct.py`

**Interfaces:**
- Produces: `PatientForm.verified_pct: Mapped[float]` (`Numeric(5,2)`, NOT NULL, default 0).

- [ ] **Step 1: Add the model column** in `patient_form.py`, immediately after `completion_pct`:

```python
    # Verified completion: fraction (0-100) of applicable-required fields the judge
    # confirmed (supported + confidence >= floor), NOT mere presence like
    # completion_pct. Set post-call by evaluate_call; drives the retry-fill gate.
    verified_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
```

- [ ] **Step 2: Generate the migration.** `just makemigration -m "add patient_form.verified_pct"` (prints a date-prefixed random-hex file).

- [ ] **Step 3: Make it idempotent.** Replace the autogenerated `op.add_column(...)` / `op.drop_column(...)` with raw guarded SQL:

```python
def upgrade() -> None:
    op.execute(
        "ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS verified_pct "
        "numeric(5, 2) NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS verified_pct")
```

- [ ] **Step 4: Apply + verify.** `just up && just migrate` then confirm the column exists (e.g. `uv run alembic upgrade head` is clean and a psql `\d patient_form` shows `verified_pct`). Then `uv run alembic downgrade -1 && uv run alembic upgrade head` round-trips cleanly.

- [ ] **Step 5: Commit.** `feat(model): patient_form.verified_pct column (idempotent migration)`

---

### Task 3: New reason + set `verified_pct` + suppressor gate

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/enums.py` (`ReviewReason`)
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/post_call_eval.py`
- Test: `vera-backend/tests/integration/test_post_call_eval.py`

**Interfaces:**
- Consumes: `satisfied_required_fraction` (Task 1), `PatientForm.verified_pct` (Task 2), `tenant.retry_fill_threshold`.
- Produces: `evaluate_call` sets `form.verified_pct` and returns review/`fill_threshold_met` when `verified_fraction >= float(tenant.retry_fill_threshold)`.

- [ ] **Step 1: Add the enum value** in `enums.py`, after `AUTO_RETRY_DISABLED`:

```python
    # Required fields remain unsatisfied and retryable, but the tenant's
    # retry_fill_threshold of applicable-required fields is already satisfied — the
    # call verified "enough", so the form parks for a human instead of redialing
    # the payer for the diminishing tail.
    FILL_THRESHOLD_MET = "fill_threshold_met"
```

- [ ] **Step 2: Write the failing integration tests** in `test_post_call_eval.py`, modeled on the file's existing fixtures (`seeded_ai_processing_form: _SeedCtx`, `fake_audit`, `fake_livekit`, `FakeLLMClient`, `EvalDeps`, `evaluate_call`, and its answer/verdict seeding helpers — same ones `test_verdict_path_mismatch_is_logged_not_silent` uses):

```python
async def test_threshold_met_routes_to_review_not_retry(
    seeded_ai_processing_form, fake_audit, fake_livekit
) -> None:
    ctx = seeded_ai_processing_form
    # Arrange: tenant.retry_fill_threshold low; seed answers + verdicts so the
    # satisfied fraction clears it while >=1 required askable field stays
    # unsatisfied (pre-change this retried). auto_retry_enabled=True.
    out = await evaluate_call(ctx.session, deps, tenant_id=ctx.tenant_id,
                              form_id=ctx.form_id, call_id=ctx.call_id, turns=[...])
    assert out.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.session.get(PatientForm, ctx.form_id)
    assert form.review_reason == ReviewReason.FILL_THRESHOLD_MET.value
    assert form.verified_pct > 0  # persisted


async def test_below_threshold_still_retries(
    seeded_ai_processing_form, fake_audit, fake_livekit
) -> None:
    ctx = seeded_ai_processing_form
    # Arrange: threshold high (e.g. 1.0) so verified fraction < threshold, a
    # retryable unsatisfied field remains, auto_retry_enabled=True.
    out = await evaluate_call(ctx.session, deps, tenant_id=ctx.tenant_id,
                              form_id=ctx.form_id, call_id=ctx.call_id, turns=[...])
    assert out.status == FormStatus.IN_QUEUE
```

Fill `deps`/`...`/arrange comments with the file's established helpers. Set the
tenant threshold via the seeded tenant row (update `retry_fill_threshold`).

- [ ] **Step 3: Run — verify fail.** `uv run pytest tests/integration/test_post_call_eval.py -k threshold -v` → FAIL (first returns IN_QUEUE; `FILL_THRESHOLD_MET`/`verified_pct` unused). (Needs `just up && just migrate`; if the local DB is wedged, note it — CI runs fresh Postgres.)

- [ ] **Step 4: Implement.** In `post_call_eval.py` add `satisfied_required_fraction` to the `forms.review` import. After `status_by_path = await load_field_status(...)` (~L471) and before the `unsatisfied = ...` block, compute + persist:

```python
    verified_fraction = satisfied_required_fraction(
        status_by_path, version.schema_json, floor=deps.floor, values=current_values
    )
    form.verified_pct = round(verified_fraction * 100, 2)
```

Then, immediately after the `if not unsatisfied:` (`ready_for_review`) block, insert the gate:

```python
    # Good-enough gate: the call verified the tenant's threshold of the applicable-
    # required fields, so park for review instead of redialing for the tail. Only
    # suppresses a retry — the guards below decide the sub-threshold case.
    if verified_fraction >= float(tenant.retry_fill_threshold):
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=unsatisfied,
            reason=ReviewReason.FILL_THRESHOLD_MET,
        )
```

- [ ] **Step 5: Run — verify pass.** Same `-k threshold` command → PASS.

- [ ] **Step 6: Full backend gate.** `just check`. Update `test_enums.py` if it snapshots `ReviewReason` members.

- [ ] **Step 7: Commit.** `feat(post-call-eval): verified-fraction retry gate + persist verified_pct`

---

### Task 4: Expose `verified_pct` on `CallSummary`

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py`
- Test: the existing `calls`/summary test module.

**Interfaces:**
- Consumes: `PatientForm.verified_pct` (Task 2).
- Produces: `CallSummary.verified_pct: float | None` (normalized via `_pct`), mirroring `completion_pct`.

- [ ] **Step 1: Write the failing test** — extend the existing list/summary test to assert the returned `CallSummary` carries `verified_pct` from the form (follow the `completion_pct` assertion pattern already there).

- [ ] **Step 2: Run — verify fail** (field absent / not populated).

- [ ] **Step 3: Implement**, mirroring `completion_pct` everywhere it appears in `calls.py`:
  - add `verified_pct: float | None = None` to the `CallSummary` response model and to `_summary(...)` (param + assignment);
  - add `PatientForm.verified_pct` to the list query `select(...)` alongside `PatientForm.completion_pct`;
  - pass `_pct(verified)` into `_summary(...)`.
  Leave the SSE frame path untouched.

- [ ] **Step 4: Run — verify pass.** Then `just check`.

- [ ] **Step 5: Commit.** `feat(api): expose verified_pct on CallSummary`

---

### Task 5: Show "Verified %" in the Overview modal

**Files:**
- Modify: `vera-frontend/src/lib/api/calls.ts` (`CallSummary`)
- Modify: `vera-frontend/src/lib/mock-data.ts` (`LiveCall`), `vera-frontend/src/lib/monitoring/liveCall.ts`
- Modify: `vera-frontend/src/components/monitoring/LiveCallModal.tsx`
- Test: `vera-frontend/src/lib/monitoring/liveCall.test.ts`, `LiveCallModal.test.tsx` (if present)

**Interfaces:**
- Consumes: `CallSummary.verified_pct` (Task 4).
- Produces: `LiveCall.verifiedProgress`; "Verified X%" rendered next to the filled %.

- [ ] **Step 1: Write the failing test** in `liveCall.test.ts` (mirror the existing `completion_pct → formProgress` test):

```typescript
it("maps verified_pct into verifiedProgress", () => {
  const live = toLiveCall(callSummary({ verified_pct: 40 }), Date.parse("2026-08-04T00:01:00Z"))
  expect(live.verifiedProgress).toBe(40)
})

it("falls back to 0 when verified_pct is null", () => {
  const live = toLiveCall(callSummary({ verified_pct: null }), Date.parse("2026-08-04T00:01:00Z"))
  expect(live.verifiedProgress).toBe(0)
})
```

Add `verified_pct: null` to the test's `callSummary()` default factory.

- [ ] **Step 2: Run — verify fail.** `npx vitest run src/lib/monitoring/liveCall.test.ts`.

- [ ] **Step 3: Implement:**
  - `calls.ts`: add `verified_pct: number | null` to `CallSummary`.
  - `mock-data.ts`: add `verifiedProgress: number` to `LiveCall`; give mock entries a value.
  - `liveCall.ts`: add `verifiedProgress: c.verified_pct ?? 0` in `toLiveCall`.
  - `LiveCallModal.tsx`: near the filled `progress` (L176), read `verified = Math.round(call?.verifiedProgress ?? 0)` and render it beside the "Patient Information Form {progress}%" as `· Verified {verified}%`.

- [ ] **Step 4: Run — verify pass.** vitest green; add/adjust a `LiveCallModal.test.tsx` assertion for the "Verified" text if that test file asserts header content.

- [ ] **Step 5: Frontend gate.** `npx tsc -b && npx eslint . && npm test && npm run build`.

- [ ] **Step 6: Commit.** `feat(monitoring-ui): show Verified % next to filled % in call overview`

---

### Task 6: Tenant-setting threshold label

**Files:**
- Modify: `vera-frontend/src/components/platform/TenantFormDialog.tsx:51`
- Test: `TenantFormDialog.test.tsx` (if it asserts the label)

- [ ] **Step 1: Update the `retry_fill_threshold` entry** in `NUMBER_FIELDS`:

```tsx
  { key: "retry_fill_threshold", label: "Min verified fraction before review (0–1)", min: 0, max: 1, step: 0.05 },
```

Thread `max={f.max}` through the rendered `<input>` (optional, alongside `min`/`step`) so fields without a `max` still render.

- [ ] **Step 2: Update/verify the test** for the new label; run `npx vitest run src/components/platform/TenantFormDialog.test.tsx`.

- [ ] **Step 3: Frontend gate.** `npx tsc -b && npx eslint . && npm test && npm run build`.

- [ ] **Step 4: Commit.** `feat(platform-ui): reword retry threshold as min verified fraction before review`

---

## Self-review notes

- **Spec coverage:** helper (T1), persisted column (T2), reason+gate+persist (T3), DTO (T4), Overview "Verified %" (T5), threshold label (T6). Edge cases (zero applicable → 1.0; `>=` boundary; verified 0 live) covered by T1 tests + the gate + T5 null-fallback. Out-of-scope items (fallback unification, critical-field override, tenant self-serve, SSE) excluded.
- **Type consistency:** helper signature matches `unsatisfied_required_paths`; `verified_pct` mirrors `completion_pct` (`Numeric(5,2)`, DTO `float | None`, frontend `number | null`); `float(tenant.retry_fill_threshold)`.
- **No placeholders:** the only `...` are in the T3 integration test where seeding must follow the target file's existing fixtures; every production edit is concrete.
- **Ordering:** T2 (column) precedes T3 (writes it) and T4 (reads it); T4 precedes T5 (frontend consumes the DTO field).
