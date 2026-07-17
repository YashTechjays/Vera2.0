# Dynamic Date-Format Normalization (Intake + Dispute-Resolve) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `type: "date"` leaf in a form schema — not just the two promoted
`patient_dob`/`appointment_date` columns — gets its submitted value validated and
normalized to the leaf's own declared `validation.date_format` (e.g. `"M/D/YYYY"`),
on both the intake path (`POST /patient-forms`) and the dispute-resolve path
(`POST /patient-forms/{id}/disputes:resolve`).

**Architecture:** The DSL already declares a `date_format` per date leaf
(`vera_core.forms.dsl.Validation.date_format`) and already has a parser for it
(`parse_date_format`) — but only `promote_columns` uses it, and only for the two
promoted columns. This plan adds the missing inverse (`format_date`, turning a
parsed `date` back into the leaf's declared format string) and a generic,
schema-driven pass over *every* date-typed leaf (`date_leaf_paths` /
`normalize_date_value` / `normalize_date_answers`) that mirrors the existing
`phone_promoted_paths` / `normalize_phone_prefix` / `normalize_phone_answers`
pattern already in `intake.py` — same shape, same call sites, same
error-wrapping contract. No schema, migration, or frontend change is needed:
research confirmed the review UI's edit-form validator already expects the
leaf's declared `date_format`, not ISO, so storing every date leaf in that format
(instead of leaving non-promoted ones as whatever raw string was submitted) is
the fix, not a new incompatibility.

**Tech Stack:** Python 3.12, pydantic v2 (`vera_core.forms.dsl`), FastAPI
(`control_plane.api.v1.patient_forms`), pytest / pytest-asyncio.

## Global Constraints

- PHI-safe: never log a field's raw value — only its path/name (existing
  `InvalidIntakeValue` contract already carries path-only; keep that).
- No `YY` (2-digit year) support — already enforced by `DATE_FORMAT_RE` in
  `dsl.py`; this plan does not touch that validator.
- No schema/catalog/migration change — every existing catalog leaf already
  declares `DATE_VALIDATION = Validation(date_format="M/D/YYYY")`; this plan
  only changes how that declared format is *used* at intake/resolve.
- No frontend change — the review UI already expects/validates the leaf's
  declared `date_format` on edit; normalizing storage to that format (not ISO)
  keeps it compatible.
- After implementation, run the `code-simplifier` agent ("simplify code") on
  the diff, then re-run `just check` (ruff + mypy --strict + pytest) before
  committing — repo-wide `CLAUDE.md` mandate, not optional here.
- Follow `packages/vera_core/src/vera_core/forms/CLAUDE.md` and the root
  `vera-backend/CLAUDE.md` PHI rules throughout.

---

### Task 1: `format_date()` — the inverse of `parse_date_format` (dsl.py)

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py` (add after
  `parse_date_format`, ~line 199, before `class Validation`)
- Test: `tests/unit/forms/test_schema_dsl.py` (add a new `TestFormatDate` class
  right after the existing `TestParseDateFormat` class, ~line 425)

**Interfaces:**
- Consumes: `_DATE_TOKEN_RE` (existing module-level regex, `dsl.py:159`), stdlib
  `datetime.date` (already imported in `dsl.py:23`).
- Produces: `format_date(value: date, date_format: str) -> str` — Task 2 imports
  and calls this from `intake.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/forms/test_schema_dsl.py`, immediately after
`class TestParseDateFormat` (which ends at the `test_never_raises_on_a_grammar_valid_repeated_token_format`
method, right before `class TestDateFormatRejectsTwoDigitYear`):

```python
class TestFormatDate:
    """`format_date` — the inverse of `parse_date_format`: renders a parsed `date`
    back into a leaf's declared display/entry `date_format`, so a date leaf's
    stored answer always matches that format regardless of how it was submitted."""

    def test_renders_m_d_yyyy_without_padding(self) -> None:
        assert format_date(date(1999, 12, 4), "M/D/YYYY") == "12/4/1999"

    def test_renders_single_digit_month_and_day_without_padding(self) -> None:
        assert format_date(date(2026, 7, 1), "M/D/YYYY") == "7/1/2026"

    def test_pads_to_mm_dd_yyyy(self) -> None:
        assert format_date(date(2026, 7, 1), "MM/DD/YYYY") == "07/01/2026"

    def test_renders_dd_mm_yyyy_with_dash_separator(self) -> None:
        assert format_date(date(1990, 12, 4), "DD-MM-YYYY") == "04-12-1990"

    def test_round_trips_through_parse_date_format(self) -> None:
        for text, fmt in [("12/4/1999", "M/D/YYYY"), ("07/01/2026", "MM/DD/YYYY")]:
            parsed = parse_date_format(text, fmt)
            assert parsed is not None
            assert format_date(parsed, fmt) == text
```

Add `format_date` to the existing `from vera_core.forms.dsl import (...)` block
at the top of the file (~line 11-17):

```python
from vera_core.forms.dsl import (
    FormSchemaDoc,
    PromotedFields,
    Validation,
    compile_document,
    format_date,
    load_document,
    parse_date_format,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py::TestFormatDate -v`
Expected: FAIL with `ImportError: cannot import name 'format_date'`

- [ ] **Step 3: Implement `format_date` in dsl.py**

Insert immediately after `parse_date_format` (which ends at line 198, right
before `class Validation` at line 201):

```python
def format_date(value: date, date_format: str) -> str:
    """Render `value` in a leaf's declared display/entry `date_format` (e.g.
    "M/D/YYYY" — see `Validation.date_format`) — the inverse of
    `parse_date_format`. Used to normalize a date leaf's stored answer to one
    consistent shape regardless of which format the submitter used (ISO from a
    machine caller, or the declared format from a human editor)."""

    def render_token(match: re.Match[str]) -> str:
        token = match.group()
        if token == "YYYY":
            return f"{value.year:04d}"
        if token == "MM":
            return f"{value.month:02d}"
        if token == "M":
            return str(value.month)
        if token == "DD":
            return f"{value.day:02d}"
        return str(value.day)  # token == "D"

    return _DATE_TOKEN_RE.sub(render_token, date_format)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: PASS (all tests in the file, including the new `TestFormatDate` class)

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): add format_date, the inverse of parse_date_format"
```

---

### Task 2: Generic per-leaf date normalization (intake.py)

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/intake.py` (add after
  `_parse_date`, ~line 211, before `unknown_payload_paths`)
- Test: `tests/unit/forms/test_intake.py`

**Interfaces:**
- Consumes: `format_date` (Task 1), `_parse_date` (existing, `intake.py:194`),
  `FormSchemaDoc.leaf_items()` (existing, `dsl.py:468`), `InvalidIntakeValue`
  (existing, `intake.py:30`).
- Produces:
  - `date_leaf_paths(doc: FormSchemaDoc) -> dict[str, str | None]` — root-anchored
    path → that leaf's declared `date_format` (`None` if undeclared).
  - `normalize_date_value(value: Any, field_path: str, date_format: str | None) -> Any`
    — validates + reformats one value; raises `InvalidIntakeValue` on failure.
  - `normalize_date_answers(answers: list[tuple[str, Any]], doc: FormSchemaDoc) -> list[tuple[str, Any]]`
    — reformats every date-leaf answer in a flattened `(path, value)` list.
  Task 3 and Task 4 call all three directly from `control_plane.api.v1.patient_forms`.

- [ ] **Step 1: Write the failing tests**

Add `format_date` and the three new names to the existing
`from vera_core.forms.intake import (...)` block at the top of
`tests/unit/forms/test_intake.py` (~line 9-19):

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    date_leaf_paths,
    iter_leaf_answers,
    missing_required,
    normalize_date_answers,
    normalize_date_value,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    required_intake_fields,
    resolve_path,
)
```

Extend the `_doc_with_promoted_fields` helper (~line 282-318) with an optional
`extra_leaves` parameter — additional, NON-promoted leaves at their own
root-anchored paths, so a test can prove normalization is keyed on `leaf.type`,
not `promoted_fields` membership (this is the same distinction
`TestPromoteColumnsPhone` already proves for phone via `leaf_types`):

```python
def _doc_with_promoted_fields(
    overrides: dict[str, str] | None = None,
    leaf_types: dict[str, str] | None = None,
    extra_leaves: dict[str, dict[str, Any]] | None = None,
) -> FormSchemaDoc:
    """A minimal v2 document promoting all eight columns (PromotedFields is total).
    `overrides` repoints individual columns; `leaf_types` repoints an individual
    promoted column's leaf `type` (default "text") — used to exercise type-specific
    promotion logic (e.g. phone). `extra_leaves` adds NON-promoted leaves at
    additional root-anchored paths (path -> leaf dict, e.g.
    "sections.patient_information.spouse_partner_dob" -> {"type": "date", ...}) —
    used to prove a behavior is keyed on `leaf.type`, not promoted_fields
    membership. system_fields (required for dsl.py validation) exactly mirror the
    merged promoted map, and every referenced path gets a context leaf."""
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
    for path, leaf in (extra_leaves or {}).items():
        _, section_key, field_key = path.split(".")
        sections.setdefault(
            section_key,
            {"title": section_key, "role": "context", "fields": {}},
        )["fields"][field_key] = leaf
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

Append these new test classes at the end of `tests/unit/forms/test_intake.py`
(after `TestPromoteColumnsPhone`):

```python
_SPOUSE_DOB_PATH = "sections.patient_information.spouse_partner_dob"


def _spouse_dob_leaf(date_format: str | None) -> dict[str, Any]:
    leaf: dict[str, Any] = {"type": "date", "title": "Spouse DOB", "role": "context"}
    if date_format is not None:
        leaf["validation"] = {"date_format": date_format}
    return leaf


class TestDateLeafPaths:
    def test_finds_every_date_typed_leaf_with_its_declared_format(self) -> None:
        doc = _doc_with_promoted_fields(
            leaf_types={"patient_dob": "date"},
            extra_leaves={_SPOUSE_DOB_PATH: _spouse_dob_leaf("M/D/YYYY")},
        )
        assert date_leaf_paths(doc) == {
            "sections.patient_information.patient_dob": None,
            _SPOUSE_DOB_PATH: "M/D/YYYY",
        }

    def test_empty_when_nothing_is_date_typed(self) -> None:
        assert date_leaf_paths(_FULL_DOC) == {}


class TestNormalizeDateValue:
    def test_reformats_iso_input_to_the_declared_format(self) -> None:
        assert normalize_date_value("1999-12-04", "path", "M/D/YYYY") == "12/4/1999"

    def test_reformats_declared_format_input_to_itself(self) -> None:
        assert normalize_date_value("12/4/1999", "path", "M/D/YYYY") == "12/4/1999"

    def test_pads_to_the_declared_format_width(self) -> None:
        assert normalize_date_value("1999-12-04", "path", "MM/DD/YYYY") == "12/04/1999"

    def test_falls_back_to_iso_when_the_leaf_declares_no_format(self) -> None:
        assert normalize_date_value("1999-12-04", "path", None) == "1999-12-04"

    def test_blank_string_passes_through_untouched(self) -> None:
        assert normalize_date_value("", "path", "M/D/YYYY") == ""

    def test_none_passes_through_untouched(self) -> None:
        assert normalize_date_value(None, "path", "M/D/YYYY") is None

    def test_raises_on_an_unparseable_value(self) -> None:
        with pytest.raises(InvalidIntakeValue) as exc:
            normalize_date_value("not-a-date", "sections.a.b", "M/D/YYYY")
        assert exc.value.field_path == "sections.a.b"


class TestNormalizeDateAnswers:
    def test_reformats_only_date_typed_paths(self) -> None:
        doc = _doc_with_promoted_fields(
            extra_leaves={_SPOUSE_DOB_PATH: _spouse_dob_leaf("M/D/YYYY")}
        )
        answers = [
            (_SPOUSE_DOB_PATH, "1999-12-04"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]
        assert normalize_date_answers(answers, doc) == [
            (_SPOUSE_DOB_PATH, "12/4/1999"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]

    def test_no_op_when_nothing_is_date_typed(self) -> None:
        answers = [("sections.patient_information.patient_name", "Jane Doe")]
        assert normalize_date_answers(answers, _FULL_DOC) == answers

    def test_raises_with_the_offending_path(self) -> None:
        doc = _doc_with_promoted_fields(
            extra_leaves={_SPOUSE_DOB_PATH: _spouse_dob_leaf("M/D/YYYY")}
        )
        answers = [(_SPOUSE_DOB_PATH, "not-a-date")]
        with pytest.raises(InvalidIntakeValue) as exc:
            normalize_date_answers(answers, doc)
        assert exc.value.field_path == _SPOUSE_DOB_PATH
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_intake.py -v`
Expected: FAIL with `ImportError: cannot import name 'date_leaf_paths'`

- [ ] **Step 3: Implement the three functions in intake.py**

Add the `format_date` import to the existing import line (~line 19):

```python
from vera_core.forms.dsl import PATH_PREFIX, FormSchemaDoc, format_date, parse_date_format
```

Insert immediately after `_parse_date` (ends at line 210, right before
`def unknown_payload_paths`):

```python
def date_leaf_paths(doc: FormSchemaDoc) -> dict[str, str | None]:
    """Root-anchored paths of every `type: "date"` leaf in `doc`, mapped to that
    leaf's declared `validation.date_format` (`None` if the leaf declares none) —
    the dynamic, schema-driven set `normalize_date_answers` reformats. Covers
    every date leaf, not just the promoted `patient_dob`/`appointment_date`
    columns `promote_columns` special-cases (mirrors `phone_promoted_paths`,
    which is deliberately scoped to promoted columns only — dates need the wider
    set because every IBV catalog schema has date leaves outside the promoted
    eight, e.g. `spouse_partner_dob`, `verified_at`)."""
    leaves = dict(doc.leaf_items())
    return {
        path: (leaf.validation.date_format if leaf.validation else None)
        for path, leaf in leaves.items()
        if leaf.type == "date"
    }


def normalize_date_value(value: Any, field_path: str, date_format: str | None) -> Any:
    """Validate `value` as a date (ISO or `date_format` — `_parse_date`'s rule)
    and reformat it to `date_format`, or to ISO if the leaf declares none — so a
    date leaf's stored answer is in one consistent shape regardless of which
    format the submitter used. Empty/blank/`None` values pass through untouched
    (a dispute-resolve caller can still submit "" to clear a date leaf). Raises
    `InvalidIntakeValue` on an unparseable value."""
    parsed = _parse_date(value, field_path, date_format)
    if parsed is None:
        return value
    return format_date(parsed, date_format) if date_format is not None else parsed.isoformat()


def normalize_date_answers(
    answers: list[tuple[str, Any]], doc: FormSchemaDoc
) -> list[tuple[str, Any]]:
    """Reformat every flattened `(path, value)` answer whose path is a date-typed
    leaf (`date_leaf_paths`) to that leaf's declared format — applied before
    `field_answer` rows are built, mirroring `normalize_phone_answers`. Non-date
    paths pass through untouched. Raises `InvalidIntakeValue` (offending path
    only, never the value) on the first unparseable date."""
    date_paths = date_leaf_paths(doc)
    if not date_paths:
        return answers
    return [
        (path, normalize_date_value(raw, path, date_paths[path]) if path in date_paths else raw)
        for path, raw in answers
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_intake.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/intake.py tests/unit/forms/test_intake.py
git commit -m "feat(forms): normalize every date leaf's answer to its declared format"
```

---

### Task 3: Wire into the intake endpoint (control_plane)

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- Test: `tests/integration/control_plane/test_patient_forms_intake.py`

**Interfaces:**
- Consumes: `date_leaf_paths`, `normalize_date_answers`, `normalize_date_value`
  (Task 2), `InvalidIntakeValue` (existing import), `CustomAPIException`,
  `DefaultExceptionCode` (existing imports).
- Produces: `_raise_422(exc: InvalidIntakeValue) -> NoReturn` — a shared helper
  Task 4 also uses; `_normalize_dates_or_422(answers, doc) -> list[tuple[str, Any]]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/control_plane/test_patient_forms_intake.py`, right
after `test_upload_creates_form_and_intake_answers` (ends at line 180, right
before `async def test_upload_promotes_worklist_columns`):

```python
async def test_upload_stores_date_answers_in_the_leafs_declared_format(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """Apps Script sends ISO ("yyyy-MM-dd") for every date field; ibv_standard
    declares "M/D/YYYY" as the display/entry format for all of them. Regression:
    `field_answer.value` must store the leaf's declared format — matching what
    the review UI's edit-form validator expects when it re-populates a date leaf
    from the stored value — not the raw ISO string as submitted."""
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        answers = {
            a.field_path: a.value
            for a in (
                await session.execute(
                    select(FieldAnswer).where(FieldAnswer.form_id == form_id)
                )
            ).scalars()
        }
        assert answers["sections.patient_information.patient_dob"] == {"value": "4/12/1990"}
        assert answers["sections.appointment_information.appointment_date"] == {
            "value": "8/3/2026"
        }


async def test_upload_rejects_invalid_date_on_a_non_promoted_date_leaf(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """spouse_partner_dob isn't one of the eight promoted columns — proves the
    fix validates every date leaf, not just patient_dob/appointment_date."""
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)
    payload = {
        **INTAKE_PAYLOAD,
        "patient_information": {
            **INTAKE_PAYLOAD["patient_information"],
            "spouse_partner_dob": "not-a-date",
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
    assert resp.json()["data"]["fields"] == ["sections.patient_information.spouse_partner_dob"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
just up  # if Postgres isn't already running for this branch's DB
uv run pytest tests/integration/control_plane/test_patient_forms_intake.py::test_upload_stores_date_answers_in_the_leafs_declared_format tests/integration/control_plane/test_patient_forms_intake.py::test_upload_rejects_invalid_date_on_a_non_promoted_date_leaf -v
```
Expected: FAIL — the first asserts `{"value": "1990-04-12"}` (today's stored ISO)
!= `{"value": "4/12/1990"}`; the second gets `200` instead of `422` (today,
`spouse_partner_dob` is stored raw, unvalidated).

- [ ] **Step 3: Wire the normalization into `upload_patient_form`**

In `apps/control_plane/src/control_plane/api/v1/patient_forms.py`, update the
`typing` import (line 20):

```python
from typing import Any, NoReturn
```

Update the `vera_core.forms.intake` import block (lines 45-55) to add the three
new names:

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    PromotedIdentifiers,
    date_leaf_paths,
    iter_leaf_answers,
    missing_required,
    normalize_date_answers,
    normalize_date_value,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    unknown_payload_paths,
)
```

Replace `_promote_or_422` (lines 110-120) with a shared `_raise_422` helper plus
the existing function rewritten to use it, and a new sibling for date answers:

```python
def _raise_422(exc: InvalidIntakeValue) -> NoReturn:
    raise CustomAPIException(
        DefaultExceptionCode.VALIDATION_ERROR,
        message="invalid field value",
        data={"fields": [exc.field_path]},
    ) from exc


def _promote_or_422(get_value: Callable[[str], Any], doc: FormSchemaDoc) -> PromotedIdentifiers:
    """`promote_columns`, translated to the API's validation-error contract — the
    error-wrapping shared by intake and dispute-resolve column promotion."""
    try:
        return promote_columns(get_value, doc)
    except InvalidIntakeValue as exc:
        _raise_422(exc)


def _normalize_dates_or_422(
    answers: list[tuple[str, Any]], doc: FormSchemaDoc
) -> list[tuple[str, Any]]:
    """`normalize_date_answers`, translated to the API's validation-error
    contract — every date-typed leaf's intake value gets reformatted to its
    declared `date_format`, not just the promoted `patient_dob`/
    `appointment_date` columns `_promote_or_422` covers."""
    try:
        return normalize_date_answers(answers, doc)
    except InvalidIntakeValue as exc:
        _raise_422(exc)
```

In `upload_patient_form`, right after `answers = normalize_phone_answers(answers, doc)`
(line 184), add the date pass before promotion:

```python
            answers = normalize_phone_answers(answers, doc)
            answers = _normalize_dates_or_422(answers, doc)
            promoted = _promote_or_422(dict(answers).get, doc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_patient_forms_intake.py -v`
Expected: PASS (all tests in the file, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/integration/control_plane/test_patient_forms_intake.py
git commit -m "feat(intake): validate + reformat every date leaf, not just promoted columns"
```

---

### Task 4: Wire into the dispute-resolve endpoint (control_plane)

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- Test: `tests/integration/control_plane/test_patient_forms_review.py`

**Interfaces:**
- Consumes: `date_leaf_paths`, `normalize_date_value` (Task 2), `_raise_422`
  (Task 3).
- Produces: nothing new consumed downstream — this is the terminal call site.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/control_plane/test_patient_forms_review.py`, right
after `test_resolve_accepts_a_date_in_the_leafs_declared_format` (ends at line
665, right before `async def test_resolve_auto_formats_missing_plus_on_insurance_phone`):

```python
async def test_resolve_reformats_a_non_promoted_date_leaf_to_its_declared_format(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    """spouse_partner_dob is a date leaf but NOT one of the eight promoted
    columns — proves the fix isn't scoped to patient_dob/appointment_date:
    resolve must reformat ANY date leaf's ISO-submitted value to the leaf's
    declared date_format ("M/D/YYYY" for ibv_standard), matching what the review
    UI's edit-form validator expects when it re-populates from the stored value."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_information.spouse_partner_dob": "1999-12-04"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        current = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == promoted_field_form,
                    FieldAnswer.field_path
                    == "sections.patient_information.spouse_partner_dob",
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert current.value == {"value": "12/4/1999"}


async def test_resolve_with_invalid_non_promoted_date_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    promoted_field_form: UUID,
) -> None:
    """Same 422 contract as the promoted-column case
    (`test_resolve_with_invalid_promoted_date_returns_422`), now for a date leaf
    that isn't one of the eight promoted columns."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_information.spouse_partner_dob": "not-a-date"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["data"]["fields"] == ["sections.patient_information.spouse_partner_dob"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/integration/control_plane/test_patient_forms_review.py::test_resolve_reformats_a_non_promoted_date_leaf_to_its_declared_format tests/integration/control_plane/test_patient_forms_review.py::test_resolve_with_invalid_non_promoted_date_returns_422 -v
```
Expected: FAIL — the first asserts a stored `{"value": "1999-12-04"}` (today,
unreformatted) instead of `{"value": "12/4/1999"}`; the second gets `200`
instead of `422`.

- [ ] **Step 3: Wire the normalization into `resolve_disputes`**

In `apps/control_plane/src/control_plane/api/v1/patient_forms.py`, add
`date_leaf_paths` and `normalize_date_value` to the import block from Task 3
(already added `date_leaf_paths`; add `normalize_date_value` alongside it so
the block reads):

```python
from vera_core.forms.intake import (
    InvalidIntakeValue,
    PromotedIdentifiers,
    date_leaf_paths,
    iter_leaf_answers,
    missing_required,
    normalize_date_answers,
    normalize_date_value,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    unknown_payload_paths,
)
```

In `resolve_disputes`, right after
`phone_paths = phone_promoted_paths(doc) if doc is not None else set()` (line 637):

```python
    phone_paths = phone_promoted_paths(doc) if doc is not None else set()
    date_paths = date_leaf_paths(doc) if doc is not None else {}
```

In the edit loop (lines 699-701), add the date branch right after the phone one:

```python
    for path, new_value in body.form_data.items():
        if path in phone_paths:
            new_value = normalize_phone_prefix(new_value)
        if path in date_paths:
            try:
                new_value = normalize_date_value(new_value, path, date_paths[path])
            except InvalidIntakeValue as exc:
                _raise_422(exc)
        cur = current_by_path.get(path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_patient_forms_review.py -v`
Expected: PASS (all tests in the file, including the two new ones and the two
pre-existing promoted-date tests `test_resolve_with_invalid_promoted_date_returns_422`
/ `test_resolve_accepts_a_date_in_the_leafs_declared_format`, unaffected by this change)

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/integration/control_plane/test_patient_forms_review.py
git commit -m "feat(resolve): validate + reformat every date leaf on dispute-resolve"
```

---

### Task 5: Full gate, simplify, final verification

**Files:** none (verification-only task)

- [ ] **Step 1: Run the full backend gate**

Run: `just check`
Expected: ruff, mypy --strict, and the full pytest suite all pass — including
every test touched/added in Tasks 1-4 and the pre-existing
`TestPromoteColumnsDateFormatFallback` / promoted-date integration tests
(unaffected, still green).

- [ ] **Step 2: Run the code-simplifier agent**

Per the repo-wide `CLAUDE.md` mandate, launch the `code-simplifier` agent
("simplify code") scoped to the files this plan touched:
`packages/vera_core/src/vera_core/forms/dsl.py`,
`packages/vera_core/src/vera_core/forms/intake.py`,
`apps/control_plane/src/control_plane/api/v1/patient_forms.py`, and the three
test files. It must not change behavior — only clarity/consistency.

- [ ] **Step 3: Re-run the full gate after simplification**

Run: `just check`
Expected: still fully green after any simplifier edits.

- [ ] **Step 4: Manual smoke check (optional but recommended given HIPAA scope)**

Boot the API (`just up` then `just api`) and re-run the intake `curl`/Apps
Script flow once against a local tenant + API key, confirming a submitted ISO
`patient_dob`/`spouse_partner_dob` comes back from
`GET /api/v1/patient-forms/{id}` already in `M/D/YYYY` — not because of this
step's code, but as a live end-to-end confirmation the wiring is correct.
