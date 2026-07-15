# Insurance provider phone auto-format design

**Date:** 2026-07-15
**Status:** Approved

## Problem

`promote_columns` (`vera_core/forms/intake.py`) stores `insurance_provider_phone_number`
with no phone-specific handling — it falls into the generic `_clean_str` branch (whitespace
trim only). The only E.164 check in the system lives in `queueability.py::ensure_queueable`,
which runs much later, at `PUT /patient-forms/{id}/status` → `IN_QUEUE`. A clinic submitting
a number without a leading `+` gets accepted at intake and only discovers the problem when
the form fails to queue, with no normalization step in between to fix the common case
(missing `+`) automatically.

## Goal

At the point a phone-type value is stored (intake, and later edits via dispute-resolve):
prepend `+` if missing (no other reformatting), then validate E.164; reject with the
existing 422 contract if still invalid. Resolve which values are phone-typed dynamically
from the schema (`leaf.type == "phone"`) — never by hardcoding a field name/path — so the
fix generalizes to any future promoted phone column with no code change. Keep
`field_answer` and the promoted `patient_form` column consistent with each other. No
backfill of existing rows — forward-only.

## Scope decisions (confirmed)

- Applies to intake **and** dispute-resolve edits (both already share `promote_columns`).
- Strict E.164 validation applies only to the **promoted, dialed** phone column
  (`insurance_provider_phone_number` today, resolved via `leaf.type == "phone"` +
  `doc.promoted_fields`). The five other schema `"phone"`-typed leaves
  (`callback_number`, `enrollment_provider_phone`, `auth_department_phone`, `pbm_phone`,
  `isp_phone`) are context-only, never dialed, and one (`callback_number`) defaults to
  `"N/A"` — validating them would break existing intake for no benefit. Out of scope.
- Backend/API only. No frontend change in this pass.
- No backfill — only forms created/edited after this ships get normalized values.

## Design

### 1. Shared regex — de-duplicate

Move `E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")` from
`control_plane/queueability.py` into `vera_core/forms/intake.py` (the natural owner of
phone-leaf semantics). `queueability.py` imports it from there instead of defining its own
copy; `voice_lab.py`'s existing import (via `queueability`) is unaffected.

### 2. Normalization + validation helpers (`vera_core/forms/intake.py`)

```python
def _normalize_phone_prefix(value: str) -> str:
    """Prepend '+' to a trimmed value that doesn't already start with one. No other
    reformatting — a value with separators/spaces still fails E164_RE downstream,
    unchanged from today's enqueue-time behavior."""

def phone_promoted_paths(doc: FormSchemaDoc) -> set[str]:
    """Paths in doc.promoted_fields whose leaf.type == 'phone' — the dynamic,
    schema-driven set of paths this fix touches. Never hardcodes a column/path name."""
```

`promote_columns`'s existing per-column dispatch (currently: date columns by name, then
`patient_name`, `chart_number`, else `_clean_str`) gains one more branch, keyed on the
leaf's type rather than the column name (mirrors how the date branch already looks up
`leaf` from `leaves.get(path)`):

```python
elif leaf is not None and leaf.type == "phone":
    cleaned = _clean_str(raw)
    if cleaned is not None:
        cleaned = _normalize_phone_prefix(cleaned)
        if not E164_RE.match(cleaned):
            raise InvalidIntakeValue(path, "expected an E.164 phone number")
    values[column] = cleaned
```

### 3. Intake endpoint (`upload_patient_form`, `patient_forms.py`)

Reordered so both `field_answer` rows and the promoted column derive from one normalized
source instead of the promoted column being computed from the raw payload independently:

1. Flatten payload → `answers` (as today, via `iter_leaf_answers`), run the existing
   `unknown_payload_paths` check.
2. Compute `phone_promoted_paths(doc)`; rewrite just those paths' values in the flat
   `answers` list through `_normalize_phone_prefix` (prefix only — no validation yet).
3. Derive `promoted = _promote_or_422(dict(answers).get, doc)` — reading from the
   now-normalized flat map instead of `resolve_path(body.intake_payload, p)`. This is where
   the E.164 validation actually raises on a bad number.
4. Build `PatientForm` from `promoted.*` (unchanged), and `field_answer` rows from the same
   normalized `answers` list — so both now carry the `+`-prefixed value.
5. Apply the same prefix fix to the matching path(s) inside `body.intake_payload` before it
   is stored verbatim as `PatientForm.intake_payload` (the raw JSON blob), via a small
   `set_path` mirroring the existing `resolve_path` helper — keeps the raw blob consistent
   with the two derived artifacts too.

### 4. Dispute-resolve (`resolve_disputes`, `patient_forms.py`)

`doc`/`version` currently get fetched *after* the per-path edit loop. Move that fetch
earlier (`form.schema_version_id` is available as soon as the form row loads), so
`phone_promoted_paths(doc)` is available inside the loop. Each `body.form_data[path]` gets
`_normalize_phone_prefix` applied before being written as a `HUMAN` `field_answer`, for
paths in that set. The existing `_promote_or_422(current_values.get, doc)` call (already
present, unchanged) validates the now-normalized current value — no new call site.

### 5. Error contract

No new shape. Reuses `InvalidIntakeValue` → `_promote_or_422` → existing 422
`VALIDATION_ERROR` with `data={"fields": [path]}`, identical to how a bad date is already
reported.

## Consequence: existing test expectation changes

`tests/unit/forms/test_intake.py` currently asserts `promote_columns` on
`" +1 555 0100 "` returns `"+1 555 0100"` unchanged (spaces preserved). Under this design
that value already has `+` (not reprefixed) but fails `E164_RE` (spaces aren't valid E.164)
— it becomes a rejection case. This is an intentional tightening, not a regression: today
that value silently reaches storage and only fails later at enqueue time; after this change
it fails fast at intake with an actionable 422 instead.

## Testing

- `tests/unit/forms/test_intake.py` — update the space-preserving case to expect
  `InvalidIntakeValue`; add cases: missing `+` + otherwise-valid digits → prefixed and
  accepted; missing `+` + invalid digits → rejected; already-`+`-prefixed valid value →
  untouched; non-phone promoted columns unaffected.
- `tests/integration/control_plane/test_patient_forms_intake.py` — new case asserting both
  `field_answer.value` (for the insurance-phone path) and
  `patient_form.insurance_provider_phone_number` carry the identical `+`-prefixed value
  after a `+`-less intake submission; existing space-containing fixture value updated to a
  valid E.164 string so it doesn't start failing for an unrelated reason.
- `tests/unit/control_plane/test_queueability.py` — `E164_RE` import path changes;
  behavior/assertions unchanged.
- New dispute-resolve test: editing the insurance phone number without a `+` results in
  both the new `HUMAN` `field_answer` row and the re-synced promoted column carrying the
  `+`-prefixed value.

## Out of scope

- The other five schema `"phone"`-typed leaves (context-only, never dialed).
- Frontend validation/formatting (`vera-frontend/src/lib/ibv/validation.ts`,
  `FieldRenderer.tsx`).
- Backfilling phone numbers on patient forms created before this change.
