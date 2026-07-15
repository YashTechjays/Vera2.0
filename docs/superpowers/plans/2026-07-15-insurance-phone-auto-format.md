# Insurance Phone Auto-Format Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-prefix `+` onto a promoted phone-typed intake value that's missing it, then validate E.164, so a clinic's `+`-less insurance provider phone number is fixed and accepted at intake/edit time instead of silently failing much later at enqueue time.

**Architecture:** All new logic is schema-driven off `leaf.type == "phone"` (never a hardcoded column/path name) inside `vera_core/forms/intake.py`, the existing pure/DB-free module that already owns `promote_columns`. Both call sites that write phone-promoted values — the intake endpoint and dispute-resolve — normalize the flat per-path answer before it becomes a `field_answer` row, and `promote_columns` (shared by both) performs the actual E.164 validation. The one existing E.164 regex (`control_plane/queueability.py`) is de-duplicated to import from the new single source of truth.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, pydantic (schema DSL), pytest (unit + integration).

## Global Constraints

- Schema-driven only: resolve which values are phone-typed via `leaf.type == "phone"` — never hardcode `insurance_provider_phone_number` or a specific path string in the new logic.
- Only the promoted, dialed phone column gets strict E.164 validation (reject on failure). The five other schema `"phone"`-typed leaves (`callback_number`, `enrollment_provider_phone`, `auth_department_phone`, `pbm_phone`, `isp_phone`) are out of scope — untouched.
- Normalization is prefix-only: prepend `+` when missing, trim surrounding whitespace. No other reformatting (no stripping internal separators).
- Applies to both intake (`POST /patient-forms`) and dispute-resolve (`POST /patient-forms/{id}/disputes:resolve`) — both already share `promote_columns`.
- No frontend change. No backfill of existing rows.
- Reuse the existing `InvalidIntakeValue` → `_promote_or_422` → 422 `VALIDATION_ERROR` contract — no new error shape.
- Backend gate: `just check` (ruff + mypy --strict + pytest) must pass before this is done. Run `code-simplifier` on the diff before the final check, per repo `CLAUDE.md`.

---

### Task 1: Phone normalization/validation helpers + `promote_columns` wiring

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/intake.py`
- Test: `vera-backend/tests/unit/forms/test_intake.py`

**Interfaces:**
- Produces (used by Task 2, 3, 4):
  - `E164_RE: re.Pattern[str]` — module-level constant.
  - `normalize_phone_prefix(value: Any) -> Any` — prepends `+` to a trimmed non-empty string missing it; non-string/blank values pass through untouched.
  - `phone_promoted_paths(doc: FormSchemaDoc) -> set[str]` — the paths among `doc.promoted_fields` whose leaf is typed `"phone"`.
  - `normalize_phone_answers(answers: list[tuple[str, Any]], doc: FormSchemaDoc) -> list[tuple[str, Any]]` — applies `normalize_phone_prefix` to just the phone-promoted paths in a flat answers list.
- Consumes: existing `_clean_str`, `InvalidIntakeValue`, `FormSchemaDoc`, `doc.leaf_items()`, `doc.promoted_fields.items()`.

- [ ] **Step 1: Write the failing tests**

Add these imports to the top of `tests/unit/forms/test_intake.py` (replacing the existing `from vera_core.forms.intake import (...)` block):

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    iter_leaf_answers,
    missing_required,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    required_intake_fields,
    resolve_path,
)
```

Replace the existing `_doc_with_promoted_fields` function (it currently hardcodes every generated leaf's `type` to `"text"`, so no test can exercise phone-typed promotion logic) with a version that accepts a per-column type override:

```python
def _doc_with_promoted_fields(
    overrides: dict[str, str] | None = None,
    leaf_types: dict[str, str] | None = None,
) -> FormSchemaDoc:
    """A minimal v2 document promoting all eight columns (PromotedFields is total).
    `overrides` repoints individual columns; `leaf_types` repoints an individual
    promoted column's leaf `type` (default "text") — used to exercise type-specific
    promotion logic (e.g. phone). system_fields (required for dsl.py validation)
    exactly mirror the merged map, and every referenced path gets a context leaf."""
    promoted_fields = {**_CANONICAL_PROMOTED, **(overrides or {})}
    leaf_types = leaf_types or {}
    sections: dict[str, Any] = {}
    for column, path in promoted_fields.items():
        _, section_key, field_key = path.split(".")
        sections.setdefault(
            section_key,
            {"title": section_key, "role": "context", "fields": {}},
        )["fields"][field_key] = {
            "type": leaf_types.get(column, "text"),
            "title": field_key,
            "role": "context",
        }
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "test_type",
            "system_fields": dict(promoted_fields),
            "promoted_fields": promoted_fields,
            "sections": sections,
            "tasks": [],
        }
    )
```

(This is backward compatible — every existing caller passes no `leaf_types`, so every generated leaf still defaults to `"text"`, unchanged.)

Append these new test classes at the end of the file:

```python
class TestNormalizePhonePrefix:
    def test_adds_plus_when_missing(self) -> None:
        assert normalize_phone_prefix("15550001234") == "+15550001234"

    def test_leaves_existing_plus_untouched(self) -> None:
        assert normalize_phone_prefix("+15550001234") == "+15550001234"

    def test_trims_surrounding_whitespace_before_checking(self) -> None:
        assert normalize_phone_prefix("  15550001234  ") == "+15550001234"

    def test_does_not_touch_internal_separators(self) -> None:
        # Adding '+' is the only reformatting — a value with internal spaces/dashes
        # still isn't E.164-shaped, and that's left to the validation step.
        assert normalize_phone_prefix("555-000-1234") == "+555-000-1234"

    def test_blank_string_passes_through_untouched(self) -> None:
        assert normalize_phone_prefix("") == ""
        assert normalize_phone_prefix("   ") == "   "

    def test_non_string_passes_through_untouched(self) -> None:
        assert normalize_phone_prefix(None) is None


class TestPhonePromotedPaths:
    def test_finds_the_phone_typed_promoted_column(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        assert phone_promoted_paths(doc) == {
            "sections.insurance_reference_information.insurance_phone_number"
        }

    def test_empty_when_no_promoted_column_is_phone_typed(self) -> None:
        assert phone_promoted_paths(_FULL_DOC) == set()


class TestNormalizePhoneAnswers:
    def test_prefixes_only_the_phone_promoted_path(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        answers = [
            ("sections.insurance_reference_information.insurance_phone_number", "15550001234"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]
        assert normalize_phone_answers(answers, doc) == [
            ("sections.insurance_reference_information.insurance_phone_number", "+15550001234"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]

    def test_no_op_when_nothing_is_phone_typed(self) -> None:
        answers = [("sections.patient_information.patient_name", "Jane Doe")]
        assert normalize_phone_answers(answers, _FULL_DOC) == answers


class TestPromoteColumnsPhone:
    """`insurance_provider_phone_number` is handled by the leaf's declared `type ==
    "phone"`, not by column name — dynamic per schema, matching every real IBV catalog
    leaf (`ibv_standard.py`'s `insurance_phone_number` is `type="phone"`). `_FULL_DOC`
    types every promoted leaf "text", so `TestPromoteColumns` above continues to
    exercise the unchanged generic path; these tests use a doc that actually types the
    column "phone"."""

    def test_missing_plus_gets_prefixed_and_accepted(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {
            "insurance_reference_information": {"insurance_phone_number": "15550001234"}
        }
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.insurance_provider_phone_number == "+15550001234"

    def test_already_prefixed_valid_number_is_untouched(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {
            "insurance_reference_information": {"insurance_phone_number": "+15550001234"}
        }
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.insurance_provider_phone_number == "+15550001234"

    def test_missing_plus_and_still_invalid_raises(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {
            "insurance_reference_information": {"insurance_phone_number": "555 000 1234"}
        }
        with pytest.raises(InvalidIntakeValue) as exc:
            promote_columns(lambda p: resolve_path(payload, p), doc)
        assert (
            exc.value.field_path
            == "sections.insurance_reference_information.insurance_phone_number"
        )

    def test_already_prefixed_but_invalid_raises(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {
            "insurance_reference_information": {"insurance_phone_number": "+1 555 0100"}
        }
        with pytest.raises(InvalidIntakeValue):
            promote_columns(lambda p: resolve_path(payload, p), doc)

    def test_absent_value_stays_none_with_no_validation_error(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        promoted = promote_columns(lambda p: None, doc)
        assert promoted.insurance_provider_phone_number is None

    def test_non_phone_typed_column_keeps_the_old_whitespace_only_behavior(self) -> None:
        # Regression guard: proves the branch is keyed on leaf.type, not the column
        # name — _FULL_DOC never types this column "phone".
        payload = {
            "insurance_reference_information": {"insurance_phone_number": " +1 555 0100 "}
        }
        promoted = promote_columns(lambda p: resolve_path(payload, p), _FULL_DOC)
        assert promoted.insurance_provider_phone_number == "+1 555 0100"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/forms/test_intake.py -v`
Expected: `ImportError: cannot import name 'normalize_phone_answers' from 'vera_core.forms.intake'` (and similar for `normalize_phone_prefix`, `phone_promoted_paths`) — the helpers don't exist yet.

- [ ] **Step 3: Implement the helpers and wire them into `promote_columns`**

In `vera-backend/packages/vera_core/src/vera_core/forms/intake.py`, change the top of the file from:

```python
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import PATH_PREFIX, FormSchemaDoc, parse_date_format

# Legacy v1 section the required-fields fallback reads structurally.
_PATIENT_INFO = "patient_information"
```

to:

```python
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import PATH_PREFIX, FormSchemaDoc, parse_date_format

# Legacy v1 section the required-fields fallback reads structurally.
_PATIENT_INFO = "patient_information"

# E.164: a leading + and 1-15 digits, first digit non-zero. Single source of truth —
# control_plane.queueability re-imports this rather than defining its own copy.
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
```

Then, right after the existing `_clean_str` function (currently at lines 139-143) and before `_parse_date`, insert:

```python
def normalize_phone_prefix(value: Any) -> Any:
    """Trim and prepend '+' to a non-empty string phone value that doesn't already
    start with one — the only reformatting this applies (no stripping of internal
    separators, so a value with spaces/dashes still fails `E164_RE` downstream,
    unchanged from before). Non-string/blank values pass through untouched, so this is
    safe to call unconditionally on any raw answer value."""
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed:
        return value
    return trimmed if trimmed.startswith("+") else f"+{trimmed}"


def phone_promoted_paths(doc: FormSchemaDoc) -> set[str]:
    """Root-anchored paths among `doc.promoted_fields` whose leaf is typed `"phone"` —
    the dynamic, schema-driven set this fix touches, resolved from the leaf's declared
    type rather than a hardcoded column/path name, so a future promoted phone column is
    covered with no code change."""
    leaves = dict(doc.leaf_items())
    return {
        path
        for _column, path in doc.promoted_fields.items()
        if (leaf := leaves.get(path)) is not None and leaf.type == "phone"
    }


def normalize_phone_answers(
    answers: list[tuple[str, Any]], doc: FormSchemaDoc
) -> list[tuple[str, Any]]:
    """Prefix '+' onto any flattened `(path, value)` answer whose path is a
    phone-typed promoted field (`phone_promoted_paths`) — applied before
    `field_answer` rows are built, so storage matches what `promote_columns` derives
    for the same path. Non-phone paths pass through untouched."""
    phone_paths = phone_promoted_paths(doc)
    if not phone_paths:
        return answers
    return [
        (path, normalize_phone_prefix(raw) if path in phone_paths else raw)
        for path, raw in answers
    ]
```

Finally, replace the body of `promote_columns` (currently):

```python
    leaves = dict(doc.leaf_items())
    values: dict[str, Any] = {}
    for column, path in doc.promoted_fields.items():
        raw = get_value(path)
        if column in ("patient_dob", "appointment_date"):
            leaf = leaves.get(path)
            date_format = leaf.validation.date_format if leaf and leaf.validation else None
            values[column] = _parse_date(raw, path, date_format)
        elif column == "patient_name":
            cleaned = _clean_str(raw)
            values[column] = cleaned.lower() if cleaned is not None else None
        elif column == "chart_number":
            cleaned = _clean_str(raw)
            values[column] = None if cleaned is not None and cleaned.upper() == "N/A" else cleaned
        else:
            values[column] = _clean_str(raw)
    return PromotedIdentifiers(**values)
```

with:

```python
    leaves = dict(doc.leaf_items())
    values: dict[str, Any] = {}
    for column, path in doc.promoted_fields.items():
        raw = get_value(path)
        leaf = leaves.get(path)
        if column in ("patient_dob", "appointment_date"):
            date_format = leaf.validation.date_format if leaf and leaf.validation else None
            values[column] = _parse_date(raw, path, date_format)
        elif column == "patient_name":
            cleaned = _clean_str(raw)
            values[column] = cleaned.lower() if cleaned is not None else None
        elif column == "chart_number":
            cleaned = _clean_str(raw)
            values[column] = None if cleaned is not None and cleaned.upper() == "N/A" else cleaned
        elif leaf is not None and leaf.type == "phone":
            cleaned = _clean_str(raw)
            if cleaned is not None:
                cleaned = normalize_phone_prefix(cleaned)
                if not E164_RE.match(cleaned):
                    raise InvalidIntakeValue(path, "expected an E.164 phone number")
            values[column] = cleaned
        else:
            values[column] = _clean_str(raw)
    return PromotedIdentifiers(**values)
```

(`leaf = leaves.get(path)` is hoisted out of the date-only branch so the new phone branch can reuse the same lookup.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/forms/test_intake.py -v`
Expected: all tests PASS, including the new `TestNormalizePhonePrefix`, `TestPhonePromotedPaths`, `TestNormalizePhoneAnswers`, `TestPromoteColumnsPhone` classes.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/forms/intake.py vera-backend/tests/unit/forms/test_intake.py
git commit -m "feat(forms): auto-format and validate phone-typed promoted fields

Schema-driven on leaf.type == 'phone' (never a hardcoded column/path name):
prepend '+' when missing, then validate E.164, inside promote_columns."
```

---

### Task 2: De-duplicate `E164_RE` in `control_plane/queueability.py`

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/queueability.py`
- Test: `vera-backend/tests/unit/control_plane/test_queueability.py` (no changes — existing tests must keep passing)

**Interfaces:**
- Consumes: `E164_RE` from Task 1 (`vera_core.forms.intake`).
- Produces: nothing new — `queueability.E164_RE` stays importable exactly as before (`voice_lab.py`'s existing `from control_plane.queueability import E164_RE` is unaffected).

- [ ] **Step 1: Run the existing test to confirm current behavior (baseline)**

Run: `just test tests/unit/control_plane/test_queueability.py -v`
Expected: all 4 existing tests PASS (this is the pre-change baseline — there's no new test to write here, since behavior doesn't change, only where the regex is defined).

- [ ] **Step 2: Replace the local regex with an import**

In `vera-backend/apps/control_plane/src/control_plane/queueability.py`, change:

```python
import re
from typing import TYPE_CHECKING

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from vera_core.integrations.credentials import get_integration_credentials

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.config.kms import KeyManagementService
    from vera_core.models import PatientForm

# E.164: a leading + and 1-15 digits, first digit non-zero. Shared with voice_lab.
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

TRUNK_INTEGRATION = "livekit_outbound_trunk_id"
```

to:

```python
from typing import TYPE_CHECKING

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from vera_core.forms.intake import E164_RE
from vera_core.integrations.credentials import get_integration_credentials

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.config.kms import KeyManagementService
    from vera_core.models import PatientForm

TRUNK_INTEGRATION = "livekit_outbound_trunk_id"
```

- [ ] **Step 3: Run the test again to confirm no regression**

Run: `just test tests/unit/control_plane/test_queueability.py -v`
Expected: same 4 tests PASS, unchanged — `E164_RE` behaves identically (same compiled pattern, single object now shared from `vera_core.forms.intake`).

- [ ] **Step 4: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/queueability.py
git commit -m "refactor(control_plane): import E164_RE from vera_core.forms.intake

De-duplicates the regex now that vera_core.forms.intake owns phone
validation for promoted fields too; behavior is unchanged."
```

---

### Task 3: Wire phone normalization into the intake endpoint

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py:45-53,156-220`
- Test: `vera-backend/tests/integration/control_plane/test_patient_forms_intake.py`

**Interfaces:**
- Consumes: `normalize_phone_answers`, `phone_promoted_paths` from Task 1 (`vera_core.forms.intake`). Drops the now-unused `resolve_path` import.
- Produces: no new public interface — `upload_patient_form`'s persisted `field_answer` rows and `PatientForm.insurance_provider_phone_number` now agree on the `+`-prefixed value.

- [ ] **Step 1: Write the failing tests**

In `vera-backend/tests/integration/control_plane/test_patient_forms_intake.py`, update the module-level `INTAKE_PAYLOAD` constant's phone value (it currently contains internal spaces, which is about to become a validation error once `promote_columns` actually validates this path — pick a clean, already-valid E.164 string so the two existing tests using it keep passing unchanged):

```python
    "insurance_reference_information": {
        "insurance_provider_name": "Demo Health Plan",
        "insurance_phone_number": "+15550100",
    },
```

(was `"insurance_phone_number": "+1 555 0100"`)

In `test_upload_promotes_worklist_columns`, update the local override and its assertion:

```python
        "insurance_reference_information": {
            "insurance_provider_name": "Blue Cross",
            "insurance_phone_number": "+15550100",
        },
```

(was `"insurance_phone_number": "+1 555 0100"`), and:

```python
        assert form.insurance_provider_phone_number == "+15550100"
```

(was `"+1 555 0100"`)

Then append two new tests at the end of the file:

```python
async def test_upload_auto_formats_missing_plus_on_insurance_phone(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """The clinic-submitted number has no leading '+' — it must be added before
    storage, and both the promoted column and the raw field_answer must agree on the
    fixed-up value (2026-07-15 design doc)."""
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    payload = {
        **INTAKE_PAYLOAD,
        "insurance_reference_information": {
            "insurance_provider_name": "Demo Health Plan",
            "insurance_phone_number": "15550100",  # no leading '+'
        },
    }
    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": payload,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one()
        assert form.insurance_provider_phone_number == "+15550100"

        answer = (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.field_path
                    == "sections.insurance_reference_information.insurance_phone_number",
                )
            )
        ).scalar_one()
        assert answer.value == {"value": "+15550100"}


async def test_upload_rejects_invalid_phone_even_after_adding_plus(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    payload = {
        **INTAKE_PAYLOAD,
        "insurance_reference_information": {
            "insurance_provider_name": "Demo Health Plan",
            "insurance_phone_number": "555 000 1234",  # still invalid once '+' is added
        },
    }
    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": payload,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["data"]["fields"] == [
        "sections.insurance_reference_information.insurance_phone_number"
    ]
```

- [ ] **Step 2: Run tests to verify the new ones fail (and confirm the fixture-value change doesn't break the old ones yet)**

Run: `just test tests/integration/control_plane/test_patient_forms_intake.py -v`
Expected: `test_upload_auto_formats_missing_plus_on_insurance_phone` and `test_upload_rejects_invalid_phone_even_after_adding_plus` FAIL (the phone number round-trips/validates exactly as submitted today — no `+` gets added, and no 422 is raised for the malformed one). The three pre-existing tests should still PASS unchanged (this endpoint hasn't changed yet, and the fixture value is already valid E.164).

*(Requires local Postgres — `just up` then `just migrate` first if not already running; these tests skip automatically without a reachable DB.)*

- [ ] **Step 3: Implement — reorder `upload_patient_form` so answers are built and phone-normalized before promotion**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`, update the import block (currently lines 45-53):

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    PromotedIdentifiers,
    iter_leaf_answers,
    missing_required,
    promote_columns,
    resolve_path,
    unknown_payload_paths,
)
```

to:

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    PromotedIdentifiers,
    iter_leaf_answers,
    missing_required,
    normalize_phone_answers,
    phone_promoted_paths,
    promote_columns,
    unknown_payload_paths,
)
```

(`resolve_path` is no longer used anywhere in this file once Step below removes its one call site — `phone_promoted_paths` is imported here for Task 4's use in the same file.)

Then replace the body of `upload_patient_form` from the `missing = missing_required(...)` check through the `session.add_all(FieldAnswer(...) for path, raw in answers)` call (currently lines 157-220):

```python
        missing = missing_required(body.intake_payload, version.schema_json)
        if missing:
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="missing required fields",
                data={"fields": missing},
            )
        doc = _v2_doc(version.schema_json)

        # Flattened + phone-normalized intake answers: one INTAKE-source field_answer per
        # provided leaf. v2 documents use root-anchored paths (`sections.…` — spec §4.2), so
        # the payload (nested by section_key) is flattened under a `sections` root. v1
        # schemas have no leaf set to validate against, so the unknown-path check and phone
        # normalization are v2-only (doc is not None); the ternary and the guard share one
        # branch. Building `answers` before promotion (rather than after) lets
        # `promote_columns` read the already-`+`-prefixed value, so field_answer and the
        # promoted column agree on it (2026-07-15 design doc).
        if doc is not None:
            payload_root: dict[str, Any] = {"sections": body.intake_payload}
            answers = list(iter_leaf_answers(payload_root))
            unrecognized = unknown_payload_paths(answers, doc)
            if unrecognized:
                raise CustomAPIException(
                    DefaultExceptionCode.VALIDATION_ERROR,
                    message="intake payload contains unknown field paths",
                    data={"fields": unrecognized},
                )
            answers = normalize_phone_answers(answers, doc)
            promoted = _promote_or_422(dict(answers).get, doc)
        else:
            payload_root = body.intake_payload
            answers = list(iter_leaf_answers(payload_root))
            promoted = PromotedIdentifiers()

        form = PatientForm(
            tenant_id=principal.tenant_id,
            schema_version_id=body.schema_version_id,
            status=FormStatus.READY_FOR_PROCESSING.value,
            intake_payload=body.intake_payload,
            patient_name=promoted.patient_name,
            patient_dob=promoted.patient_dob,
            appointment_date=promoted.appointment_date,
            chart_number=promoted.chart_number,
            appointment_type=promoted.appointment_type,
            member_id=promoted.member_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
            completion_pct=0,
            retry_count=0,
        )
        session.add(form)
        await session.flush()

        session.add_all(
            FieldAnswer(
                tenant_id=principal.tenant_id,
                form_id=form.id,
                call_id=None,
                field_path=path,
                value={"value": raw},
                source=AnswerSource.INTAKE.value,
                confidence=None,
                evidence_seq=None,
                evidence=None,
                is_current=True,
            )
            for path, raw in answers
        )
```

Note: this reorders the "unknown field paths" 422 to fire before per-field promotion validation (previously promotion ran first). Both were already independent 422s: this only changes which one wins if a payload somehow has both problems at once — not exercised by any existing test, and no test in Step 1 depends on the old order.

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/integration/control_plane/test_patient_forms_intake.py -v`
Expected: all tests PASS, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py vera-backend/tests/integration/control_plane/test_patient_forms_intake.py
git commit -m "feat(control_plane): normalize phone-promoted answers at intake

Builds and phone-normalizes the flat field_answer list before promotion,
so promote_columns validates the already-'+'-prefixed value and
field_answer/patient_form agree on it."
```

---

### Task 4: Wire phone normalization into dispute-resolve

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py:604-745`
- Test: `vera-backend/tests/integration/control_plane/test_patient_forms_review.py`

**Interfaces:**
- Consumes: `phone_promoted_paths` (already imported in Task 3), `normalize_phone_prefix` from Task 1 (`vera_core.forms.intake`).
- Produces: no new public interface — a dispute-resolve edit to a phone-promoted path now gets the same `+`-prefix-then-validate treatment as intake.

- [ ] **Step 1: Write the failing tests**

In `vera-backend/tests/integration/control_plane/test_patient_forms_review.py`, add this constant near `INSURANCE_PROVIDER_NAME` (currently line 188):

```python
INSURANCE_PHONE = "sections.insurance_reference_information.insurance_phone_number"
```

Then append two new tests after `test_resolve_accepts_a_date_in_the_leafs_declared_format` (or anywhere in the `resolve` test section):

```python
async def test_resolve_auto_formats_missing_plus_on_insurance_phone(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PHONE: "15550100"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider_phone_number == "+15550100"

        answer = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == promoted_field_form,
                    FieldAnswer.field_path == INSURANCE_PHONE,
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert answer.value == {"value": "+15550100"}
        assert answer.source == "human"


async def test_resolve_with_invalid_promoted_phone_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PHONE: "555 000 1234"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["data"]["fields"] == [INSURANCE_PHONE]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/integration/control_plane/test_patient_forms_review.py -v -k "insurance_phone or invalid_promoted_phone"`
Expected: both new tests FAIL (`test_resolve_auto_formats_missing_plus_on_insurance_phone` — the stored value is `"15550100"`, no `+` added; `test_resolve_with_invalid_promoted_phone_returns_422` — resolves 200 instead of 422, since nothing validates this path yet).

- [ ] **Step 3: Implement — normalize phone-promoted edits before they're written**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`, in `resolve_disputes` (currently starting at line 604), move the `version`/`doc` lookup from its current position (right before the final `if doc is not None: promoted = ...` block, currently lines 731-736) to immediately after the form-not-found check, and compute `phone_paths` there. Change:

```python
    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    # Open disputes BEFORE any writes: only an actually-disputed path may emit a
    # `dispute_action` (a pre-call/baseline edit advances the baseline without one).
    open_paths = await _open_dispute_paths(session, form_id)
```

to:

```python
    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    # Fetched here (not after the edit loop, as before) so phone-typed promoted paths
    # are known before normalizing incoming edits below (2026-07-15 design doc).
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    doc = _v2_doc(version.schema_json)
    phone_paths = phone_promoted_paths(doc) if doc is not None else set()

    # Open disputes BEFORE any writes: only an actually-disputed path may emit a
    # `dispute_action` (a pre-call/baseline edit advances the baseline without one).
    open_paths = await _open_dispute_paths(session, form_id)
```

Then in the per-path edit loop, change:

```python
    for path, new_value in body.form_data.items():
        cur = current_by_path.get(path)
```

to:

```python
    for path, new_value in body.form_data.items():
        if path in phone_paths:
            new_value = normalize_phone_prefix(new_value)
        cur = current_by_path.get(path)
```

Finally, remove the now-duplicate `version`/`doc` fetch later in the function. Change:

```python
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    doc = _v2_doc(version.schema_json)
    # Re-derive promoted patient_form columns from the post-write current answers —
    # any resolve call that changes a promoted field's value (dispute or plain edit)
    # keeps the worklist columns in sync, not just intake (2026-07-10 design doc).
    if doc is not None:
```

to:

```python
    # doc/phone_paths already resolved above. Re-derive promoted patient_form columns
    # from the post-write current answers — any resolve call that changes a promoted
    # field's value (dispute or plain edit) keeps the worklist columns in sync, not
    # just intake (2026-07-10 design doc).
    if doc is not None:
```

Also add `normalize_phone_prefix` to the existing `vera_core.forms.intake` import block in this file (from Task 3's edit):

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    PromotedIdentifiers,
    iter_leaf_answers,
    missing_required,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    unknown_payload_paths,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/integration/control_plane/test_patient_forms_review.py -v`
Expected: all tests PASS, including the two new ones, with no regressions in the rest of the file (in particular `test_resolve_promotes_the_patient_form_column`, `test_resolve_leaves_promoted_columns_untouched_for_a_non_promoted_field`, `test_resolve_with_invalid_promoted_date_returns_422`, `test_resolve_accepts_a_date_in_the_leafs_declared_format` — none of these touch the insurance phone path, so `phone_paths` normalization is a no-op for them).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py vera-backend/tests/integration/control_plane/test_patient_forms_review.py
git commit -m "feat(control_plane): normalize phone-promoted edits in dispute-resolve

Moves the schema/doc lookup earlier so incoming edits to a phone-typed
promoted path get the same '+'-prefix-then-validate treatment as intake."
```

---

### Task 5: Simplify pass + full verification

**Files:** none new — this task runs tooling over the diff from Tasks 1-4.

- [ ] **Step 1: Run the code-simplifier agent**

Per the repo root `CLAUDE.md`, run the `code-simplifier` agent (`code-simplifier@claude-plugins-official`) on the files touched in Tasks 1-4:
- `vera-backend/packages/vera_core/src/vera_core/forms/intake.py`
- `vera-backend/apps/control_plane/src/control_plane/queueability.py`
- `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- `vera-backend/tests/unit/forms/test_intake.py`
- `vera-backend/tests/integration/control_plane/test_patient_forms_intake.py`
- `vera-backend/tests/integration/control_plane/test_patient_forms_review.py`

It must not change behavior — only clarity/consistency.

- [ ] **Step 2: Re-run the full backend gate**

Run: `just check` (from `vera-backend/`)
Expected: ruff (lint), mypy --strict (typecheck), and the full pytest suite all PASS.

- [ ] **Step 3: Commit any simplifier changes**

```bash
git add -A
git commit -m "refactor: apply code-simplifier pass to insurance-phone-auto-format diff"
```

(Skip this commit if the simplifier made no changes.)
