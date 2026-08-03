# Tenant Retry Fill-Threshold Gate — Design

**Date:** 2026-08-04
**Status:** Approved design, pending implementation plan
**Owner:** Yash

## Goal

Make the post-call **auto-retry decision** honor a per-tenant **satisfaction
threshold**: if a call already verified at least `retry_fill_threshold` of the
form's applicable-required fields, send the form to human review instead of
redialing the payer. Below the threshold, retry as today. The threshold is a
platform-operator-controlled, per-tenant value (default **0.50**).

## Background

The retry decision lives in `evaluate_call`
(`vera-backend/packages/vera_core/src/vera_core/services/post_call_eval.py`,
decision block ~L463–520). Today it is **binary and satisfaction-based**: if
*any* required-applicable field is unsatisfied and askable (and budget remains,
the call wasn't user-ended, and both auto-retry gates are on), it retries.

`tenant.retry_fill_threshold` (`Numeric(4,3)`, default `0.50`, CHECK `BETWEEN 0
AND 1`) already exists
(`vera-backend/packages/vera_core/src/vera_core/models/tenant.py:44`) but the
**eval path ignores it**. It is only read by the *fallback* resolver
`resolve_ai_processing` (`control_plane/post_call.py:96`,
`completion_pct < retry_fill_threshold*100`), which runs only when the eval
pipeline is bypassed/disabled.

The config surface already exists and is **operated by platform operators per
tenant**:
- `POST /platform/tenants/{id}/retry-config` and `PATCH /platform/tenants/{id}`
  (`control_plane/api/v1/platform_tenants.py`, `platform_require(TENANTS_MANAGE)`,
  written through the `platform_tenant_config.py` SECURITY DEFINER fn, audited).
- API + DB validation `0–1`.
- Frontend control in `vera-frontend/src/components/platform/TenantFormDialog.tsx`
  ("Retry fill threshold (0–1)", step 0.05).

So the **only missing piece is the decision logic**; storage, endpoint, audit,
validation, and UI are done.

## Proposed change

### 1. The metric (new helper in `forms/review.py`)

Reuse the existing applicability machinery:

```python
def satisfied_required_fraction(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any],
) -> float:
    """Fraction (0.0–1.0) of required, applicable fields that are satisfied.

    Satisfaction is the same standard the retry gates use (is_field_satisfied:
    AI answers need supported=True and confidence >= floor; intake/human need a
    value). A form with no applicable-required fields is complete → 1.0.
    """
    applicable = _required_paths(schema_json, values, askable_only=False)
    if not applicable:
        return 1.0
    unsatisfied = unsatisfied_required_paths(
        status_by_path, schema_json, floor=floor, values=values
    )
    return (len(applicable) - len(unsatisfied)) / len(applicable)
```

- **Denominator** = `_required_paths(..., askable_only=False)` — required leaves
  whose conditional branch is active for the current answers (a "not covered"
  answer collapses that branch out). Same set `unsatisfied_required_paths` is
  computed against, so `unsatisfied ⊆ applicable` always holds.
- **Numerator** = applicable − unsatisfied = satisfied applicable-required.
- Deliberately **satisfaction-based**, not `form_completion_pct` (which only
  checks value *presence*). This is consistent with the judge-coverage fix: a
  field counts only if verified (`supported` + `confidence ≥ floor`).

### 2. The decision (one new gate in `evaluate_call`)

Insert a suppressor gate immediately after the existing "all satisfied" check
and before the retry gates. Order becomes:

1. **All required satisfied** (`not unsatisfied`) → `EXCEPTION_REVIEW`,
   `ready_for_review`. *(unchanged)*
2. **`satisfied_required_fraction(...) >= tenant.retry_fill_threshold`** →
   `EXCEPTION_REVIEW`, **`fill_threshold_met`**. *(new — "good enough")*
3. Else, existing retry gates unchanged:
   - `retryable and can_retry` and not `user_ended` and both auto-retry gates on
     → `IN_QUEUE` (`"retry"`).
   - Otherwise the existing review reasons (`retries_exhausted`,
     `unsatisfied_unaskable`, `auto_retry_disabled`, `user_ended`).

The threshold only ever **suppresses** a retry that would otherwise happen; it
never forces one, and it never overrides the askable / budget / user-ended /
auto-retry guards. Because retries are focused, the loop still terminates
naturally when the fraction crosses the threshold, budget exhausts, or nothing
askable remains.

Boundary: comparison is **`>=`** — a form exactly at the threshold is "good
enough" (does not retry).

### 3. Config — no new plumbing

Reuse `tenant.retry_fill_threshold` (default 0.50, range `0–1`,
platform-operator-editable via the existing `/retry-config` endpoint and
`TenantFormDialog`). Endpoint semantics are intuitive:
- **0.0** → `satisfied_fraction >= 0` is always true → never retry on threshold.
- **1.0** → retry until every applicable-required field is satisfied (closest to
  today's behavior).
- **0.50** (default) → retry while less than half the applicable-required work is
  verified.

### 4. Observability — new review reason

Add `ReviewReason.FILL_THRESHOLD_MET = "fill_threshold_met"`
(`vera-backend/packages/vera_core/src/vera_core/models/enums.py`) so QA can
distinguish "stopped: good-enough" from "stopped: fully satisfied"
(`ready_for_review`). Emitted in the `_finish(...)` audit `detail.reason` like
the other reasons.

### 5. UI — copy tweak only

The control already exists. Update the label/help text in `TenantFormDialog.tsx`
so it reads as a satisfaction threshold rather than raw fill, e.g.
**"Min verified % before sending to review instead of retrying"**, and consider
rendering as a percentage (stored as the existing 0–1 decimal). No new endpoint,
authz, or validation.

## Edge cases

- **Zero applicable-required fields** → helper returns `1.0` → gate 2 sends to
  review; no divide-by-zero.
- **Threshold at endpoints** (0.0 / 1.0) behave as described above.
- **Unaskable tail below threshold**: if `satisfied_fraction < threshold` but no
  unsatisfied field is askable, the existing `retryable` guard still routes to
  `unsatisfied_unaskable` review — the threshold does not cause a doomed redial.

## Components / files

- `packages/vera_core/src/vera_core/forms/review.py` — add
  `satisfied_required_fraction` helper.
- `packages/vera_core/src/vera_core/models/enums.py` — add
  `ReviewReason.FILL_THRESHOLD_MET`.
- `packages/vera_core/src/vera_core/services/post_call_eval.py` — insert gate 2
  in `evaluate_call`.
- `vera-frontend/src/components/platform/TenantFormDialog.tsx` — label/help copy.
- Tests (see below).

**Untouched (deliberately):** the config storage/endpoint/definer-fn/audit, and
the fallback resolver `post_call.py` (see Out of scope).

## Data flow

Call ends → `AI_PROCESSING` → `evaluate_call`: extract → judge → write
`FieldEvaluation`s → `load_field_status` → compute `unsatisfied` →
**compute `satisfied_required_fraction` and compare to
`tenant.retry_fill_threshold`** → transition (`fill_threshold_met` review, or
retry, or existing reasons) → audit + `try_dispatch`.

## Error handling

No new failure modes. The helper is pure and total (returns `1.0` on empty
denominator). The threshold read is a plain column already loaded on `tenant`.
All existing guards (status guard, stale-call guard, LLM-error routing) are
unchanged.

## Testing strategy

Unit (pure helper, `tests/unit/.../forms/`):
- fraction with mixed satisfied/unsatisfied applicable-required fields
- conditional collapse: a "not covered" answer drops a branch from the
  denominator, changing the fraction
- zero applicable-required → 1.0
- AI answer without a supporting evaluation counts as unsatisfied (ties to the
  judge-coverage fix)

Integration (`evaluate_call`, existing `test_post_call_eval.py` patterns):
- below threshold + askable + budget + auto-retry on → `IN_QUEUE`
- at/above threshold → `EXCEPTION_REVIEW` / `fill_threshold_met` (no retry)
- threshold 1.0 reproduces today's "retry while anything unsatisfied" behavior
- threshold 0.0 never retries on threshold
- below threshold but unaskable tail → `unsatisfied_unaskable` (no redial)
- `fill_threshold_met` appears in the audit `detail.reason`

Frontend: existing `TenantFormDialog` test updated for the new copy.

## Out of scope (future)

- **Critical-field override** (some fields force a retry regardless of %) —
  intentionally deferred; v1 is flat weighting. Revisit if review data shows
  high-% forms slipping through missing key fields.
- **Unifying the fallback metric**: `post_call.py` keeps reading the same
  `retry_fill_threshold` against raw `completion_pct`. Two slightly different
  metrics share one value; the eval path dominates in practice (fallback runs
  only when eval is disabled). Left as-is for v1; note it in the PR.
- **Tenant-admin self-serve editing** — stays platform-operator-only,
  consistent with the existing per-tenant retry knobs.
