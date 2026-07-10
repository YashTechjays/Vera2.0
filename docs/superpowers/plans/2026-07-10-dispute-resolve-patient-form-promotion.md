# Dispute-resolve → patient_form column promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `patient_form` promoted columns (`patient_name`, `patient_dob`, `appointment_date`, `chart_number`, `appointment_type`, `member_policy_id`, `insurance_provider`, `insurance_provider_phone_number`) stay in sync with the current field answer whenever a value changes — at intake AND when a human resolves a dispute — driven by a schema-declared mapping instead of hand-maintained Python literals.

**Architecture:** A new `promoted_fields: dict[str, str]` block on the schema DSL (`FormSchemaDoc`) declares column → schema-path mappings, validated as a subset of the existing `system_fields` block (so a promoted column can never be legitimately empty at intake). `promote_columns` becomes schema-driven and takes a value-getter closure, so the same function serves both the nested intake payload and the flat `{field_path: value}` map `resolve_disputes` already builds. The dead `PatientForm.member_id` column is dropped along the way.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, Alembic, pytest, uv workspace (`vera-backend`); TypeScript/React (`vera-frontend`, one type-only edit).

## Global Constraints

- Full spec: `docs/superpowers/specs/2026-07-10-dispute-resolve-patient-form-promotion-design.md` — read it before starting; every task below implements one of its sections.
- Backend gate before any task is "done": `cd vera-backend && just check` (ruff format --check, ruff check, mypy --strict, pytest). Run it at minimum at the end of every task that touches `vera-backend/`.
- Frontend gate for the one frontend task: `cd vera-frontend && npm run build && npm run lint`.
- Migration revision IDs are alembic's auto-generated random hex — never hand-number them. Use `just makemigration "<message>"` and edit the generated file's body, never its `revision`/`down_revision` fields (except the one explicit relink in Task 1).
- Every `ALTER TABLE` in a new migration must be idempotent (`ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF EXISTS`) — see the migrations bullet in `vera-backend/CLAUDE.md`.
- Per the repo-root `CLAUDE.md`: after the last code task, run the `code-simplifier` agent ("simplify code") over the changes in this same session, then re-run `just check`, before treating the plan as complete (Task 7).
- PHI discipline (`vera-backend/CLAUDE.md`, `apps/control_plane/.../CLAUDE.md`): none of these changes log or trace field values; don't introduce any `logger.*`/print of a promoted column's value.

---

### Task 1: Fix the migration heads left by the rebase

**Files:**
- Modify: `vera-backend/migrations/versions/20260709_1834_8115d1763daf_add_ivr_navigation_enabled_to_patient_.py`

**Interfaces:** None — this is a pure migration-graph fix, no code interface changes. Every later task that runs an integration test depends on this: right now `uv run alembic upgrade head` fails with "Multiple head revisions are present."

- [ ] **Step 1: Confirm the current broken state**

Run (from `vera-backend/`): `uv run alembic heads`
Expected output (two heads — this is the bug):
```
8115d1763daf (head)
089b3e98f0b0 (head)
```

- [ ] **Step 2: Relink the orphaned migration onto the true head**

Edit `vera-backend/migrations/versions/20260709_1834_8115d1763daf_add_ivr_navigation_enabled_to_patient_.py`:

```python
revision: str = "8115d1763daf"
down_revision: str | None = "089b3e98f0b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

(Only the `down_revision` value changes, from `"efa94eaaf3f9"` to `"089b3e98f0b0"`.)

- [ ] **Step 3: Verify a single linear head**

Run: `uv run alembic heads`
Expected output: exactly one line, `8115d1763daf (head)`.

- [ ] **Step 4: Wipe and recreate the local dev database**

This branch's migrations aren't shared yet, so per project direction we relink history by hand rather than adding a merge-revision — which means the local DB's `alembic_version` bookkeeping no longer matches this rewritten chain. Reset it:

Run (from `vera-backend/`):
```bash
docker compose down -v
just up
just migrate
```
Expected: `just migrate` ends with `alembic upgrade head` completing with no errors (last log line references revision `8115d1763daf` or later).

- [ ] **Step 5: Confirm integration tests can now run at all**

Run: `uv run pytest tests/integration/control_plane/test_patient_forms_intake.py -q`
Expected: tests execute (pass or fail on their own merits) — no more `Failed: alembic upgrade of vera_test failed: ... Multiple head revisions`.

- [ ] **Step 6: Commit**

```bash
cd vera-backend
git add migrations/versions/20260709_1834_8115d1763daf_add_ivr_navigation_enabled_to_patient_.py
git commit -m "fix(migrations): relink ivr_navigation_enabled migration onto the true head

Rebase left two unmerged alembic heads (8115d1763daf branching off an
earlier revision than the dev merge chain). Branch migrations aren't
shared yet, so relink down_revision for one linear chain instead of a
merge-revision file; local DB reset separately, not via migration."
```

---

### Task 2: Add `promoted_fields` to the schema DSL

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py`
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Produces: `vera_core.forms.dsl.PROMOTABLE_COLUMNS: frozenset[str]` — the 8 valid `patient_form` column names. `FormSchemaDoc.promoted_fields: dict[str, str] | None` — column name → root-anchored leaf path.
- Consumes: nothing new (uses the existing `leaves` dict and `system_fields` already computed inside `_validate_document`).

- [ ] **Step 1: Write the failing tests**

In `vera-backend/tests/unit/forms/test_schema_dsl.py`, add these methods to `class TestDocumentValidation` (after `test_confirm_immediate_anchor_through_nested_group_gate`, i.e. at the end of the class):

```python
    def test_promoted_fields_accepts_a_valid_subset_of_system_fields(self) -> None:
        doc = minimal_doc(
            system_fields={"plan_type": "sections.basics.plan_type"},
            promoted_fields={"patient_name": "sections.basics.plan_type"},
        )
        FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_unknown_column(self) -> None:
        doc = minimal_doc(
            system_fields={"plan_type": "sections.basics.plan_type"},
            promoted_fields={"not_a_column": "sections.basics.plan_type"},
        )
        with pytest.raises(ValidationError, match="not a promotable patient_form column"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_path_not_a_leaf(self) -> None:
        doc = minimal_doc(
            system_fields={"plan_type": "sections.basics.plan_type"},
            promoted_fields={"patient_name": "sections.basics.missing"},
        )
        with pytest.raises(ValidationError, match="does not resolve to a leaf"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_path_not_backed_by_system_fields(self) -> None:
        doc = minimal_doc(promoted_fields={"patient_name": "sections.basics.plan_type"})
        with pytest.raises(ValidationError, match="not a system_fields target"):
            FormSchemaDoc.model_validate(doc)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k promoted_fields -v`
Expected: 4 failures. The "rejects" tests fail because pydantic's `extra="forbid"` raises `ValidationError` for the unrecognized `promoted_fields` key with a message like "Extra inputs are not permitted" — which does not match the expected regex (`not a promotable patient_form column`, etc.), so `pytest.raises(..., match=...)` itself fails. The "accepts" test fails outright with the same extra-field error.

- [ ] **Step 3: Add `PROMOTABLE_COLUMNS`**

In `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py`, right after the `COLLECTED_ROLES` constant:

```python
RANGE_TYPES: frozenset[str] = frozenset({"currency", "percent", "integer"})
COLLECTED_ROLES: frozenset[str] = frozenset({"ask", "confirm"})
# patient_form columns a schema may declare in `promoted_fields` (dsl.py `_validate_document`
# below, and vera_core.forms.intake.promote_columns). member_id is excluded — it's dead
# (never populated by any code path; see PatientForm.member_id docstring history).
PROMOTABLE_COLUMNS: frozenset[str] = frozenset({
    "patient_name",
    "patient_dob",
    "appointment_date",
    "chart_number",
    "appointment_type",
    "member_policy_id",
    "insurance_provider",
    "insurance_provider_phone_number",
})
```

- [ ] **Step 4: Add the `promoted_fields` field to `FormSchemaDoc`**

In the same file, in `class FormSchemaDoc`, right after `system_fields`:

```python
class FormSchemaDoc(_Model):
    dsl_version: Literal["2.1"]
    name: str
    insurance_type: str
    description: str | None = None
    system_fields: dict[str, str] | None = None
    # patient_form column name -> root-anchored leaf path. Always a subset of
    # system_fields (validated below) — a promoted column can never be legitimately
    # empty, since system_fields targets are exactly what required_intake_fields
    # enforces at creation (intake.py).
    promoted_fields: dict[str, str] | None = None
    # Session-wide STT vocabulary, fed verbatim to deepgram.STTv2(keyterms=...)
    # at voice-session build; applies to every task. Static domain terms only.
    stt_key_terms: list[str] | None = None
```

- [ ] **Step 5: Add the validator block**

In the same file, inside `_validate_document`, right after the `# system fields` block (the loop that checks `self.system_fields`) and before the `# stt key terms` comment:

```python
        # system fields
        for handle, path in (self.system_fields or {}).items():
            check_key(f"system_fields {handle}", handle)
            if path not in leaves:
                errors.append(f"system_fields.{handle}: {path!r} does not resolve to a leaf")

        # promoted fields — patient_form columns re-derived from the current answer at
        # dispute-resolve time too (not just intake). Must be a subset of system_fields:
        # that's what guarantees a promoted column is never legitimately empty.
        system_field_paths = set((self.system_fields or {}).values())
        for column, path in (self.promoted_fields or {}).items():
            if column not in PROMOTABLE_COLUMNS:
                errors.append(
                    f"promoted_fields.{column}: not a promotable patient_form column "
                    f"(one of {sorted(PROMOTABLE_COLUMNS)})"
                )
            if path not in leaves:
                errors.append(f"promoted_fields.{column}: {path!r} does not resolve to a leaf")
            elif path not in system_field_paths:
                errors.append(
                    f"promoted_fields.{column}: {path!r} is not a system_fields target "
                    "(promoted fields must be guaranteed present at intake)"
                )

        # stt key terms: bounded, unique, static vocabulary
```

(Only the new `# promoted fields` block and its four lines of context are additions — the `# system fields` block above and the `# stt key terms` comment below already exist and are shown only to anchor the insertion point.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: all tests pass, including the 4 new ones and every pre-existing test in the file (this change is additive/optional-field, so nothing else should move).

- [ ] **Step 7: Type-check and lint**

Run: `uv run mypy packages/vera_core/src/vera_core/forms/dsl.py && uv run ruff check packages/vera_core/src/vera_core/forms/dsl.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): add promoted_fields to the schema DSL

A schema declares which patient_form columns its fields promote to,
validated as a subset of system_fields so a promoted column can never
be legitimately empty at intake. No consumer wired yet."
```

---

### Task 3: Declare `promoted_fields` in both catalogs

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/disease_only.py`
- Modify (generated): `vera-backend/data/form_schemas/ibv_form_standard_v2.json`
- Modify (generated): `vera-backend/data/form_schemas/disease_only_verification.json`
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py`
- Modify: `vera-backend/tests/unit/forms/test_conditions.py` (comment only)

**Interfaces:**
- Consumes: `FormSchemaDoc.promoted_fields` (Task 2).
- Produces: `SCHEMAS["infertility_treatment"][1]().promoted_fields` and `SCHEMAS["disease_only"][1]().promoted_fields` — both non-empty from here on; Task 4/5 read these.

- [ ] **Step 1: Add `promoted_fields` to `build_ibv_standard`, and drop the redundant `policy_id` handle**

In `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`, the `system_fields={...}` block inside `build_ibv_standard()` currently ends (around line 1033) with:

```python
        system_fields={
            "chart_number": "sections.patient_information.chart_number",
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "patient_gender": "sections.patient_information.patient_gender",
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "member_id": "sections.insurance_information.policy_number",
            "policy_id": "sections.insurance_information.policy_number",
            "insurance_provider_name": "sections.insurance_reference_information.insurance_provider_name",
            "insurance_provider_phone_number": "sections.insurance_reference_information.insurance_phone_number",
            "verified_by": "sections.verification_information.verified_by",
            "form_queued_by": "sections.verification_information.verified_by",
            "callback_number": "sections.verification_information.callback_number",
            "hospital_name": "sections.hospital_information.hospital_name",
            "hospital_address": "sections.hospital_information.hospital_address",
            "hospital_tax_id": "sections.hospital_information.tax_id",
            "hospital_npi": "sections.hospital_information.npi",
            "doctor_name": "sections.provider_reference_information.provider_name",
            "doctor_npi": "sections.provider_reference_information.npi",
        },
```

Also remove the `"policy_id"` line — it's a pure duplicate of `"member_id"` (same path, no
distinct consumer: nothing renders `{{policy_id}}`, only `{{member_id}}` is spoken, in this
same file's introduction task prompt). Keeping both is confusing; `"member_id"` alone
already keeps `required_intake_fields` behavior identical (it dedupes by path). Add a
`promoted_fields={...}` block directly after (before `stt_key_terms=[`):

```python
        system_fields={
            "chart_number": "sections.patient_information.chart_number",
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "patient_gender": "sections.patient_information.patient_gender",
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "member_id": "sections.insurance_information.policy_number",
            "insurance_provider_name": "sections.insurance_reference_information.insurance_provider_name",
            "insurance_provider_phone_number": "sections.insurance_reference_information.insurance_phone_number",
            "verified_by": "sections.verification_information.verified_by",
            "form_queued_by": "sections.verification_information.verified_by",
            "callback_number": "sections.verification_information.callback_number",
            "hospital_name": "sections.hospital_information.hospital_name",
            "hospital_address": "sections.hospital_information.hospital_address",
            "hospital_tax_id": "sections.hospital_information.tax_id",
            "hospital_npi": "sections.hospital_information.npi",
            "doctor_name": "sections.provider_reference_information.provider_name",
            "doctor_npi": "sections.provider_reference_information.npi",
        },
        # patient_form columns re-derived from the current answer at intake AND
        # dispute-resolve (2026-07-10 design doc). Every path here must also be a
        # system_fields target (dsl.py validates this).
        promoted_fields={
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "chart_number": "sections.patient_information.chart_number",
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "member_policy_id": "sections.insurance_information.policy_number",
            "insurance_provider": "sections.insurance_reference_information.insurance_provider_name",
            "insurance_provider_phone_number": (
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        },
```

- [ ] **Step 2: Add `promoted_fields` to `build_disease_only`, and drop the redundant `policy_id` handle**

In `vera-backend/packages/vera_core/src/vera_core/forms/catalog/disease_only.py`, the `system_fields={...}` block inside `build_disease_only()` currently is:

```python
        system_fields={
            "chart_number": "sections.patient_information.chart_number",
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "patient_gender": "sections.patient_information.patient_gender",
            "member_id": "sections.policy_details.policy_number",
            "policy_id": "sections.policy_details.policy_number",
            "verified_by": "sections.verification_information.verified_by",
            "callback_number": "sections.verification_information.callback_number",
            "form_completed_at": "sections.verification_information.verified_at",
        },
```

Same duplicate-handle cleanup as `ibv_standard.py`: drop `"policy_id"` (this schema has no
prompt referencing `{{policy_id}}` either — grep confirms zero uses — so it's removed
outright, not kept for a live consumer). Add a `promoted_fields={...}` block directly after
(before `shared_conditions={`). This schema has no appointment/insurance-reference
sections, so only 3 columns are promotable here — the corresponding `patient_form` columns
stay `None`, same as today:

```python
        system_fields={
            "chart_number": "sections.patient_information.chart_number",
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "patient_gender": "sections.patient_information.patient_gender",
            "member_id": "sections.policy_details.policy_number",
            "verified_by": "sections.verification_information.verified_by",
            "callback_number": "sections.verification_information.callback_number",
            "form_completed_at": "sections.verification_information.verified_at",
        },
        # Only the columns this schema actually collects — no appointment/insurance
        # sections exist here, so appointment_date/appointment_type/member_policy_id/
        # insurance_provider* stay unmapped (their patient_form columns stay None).
        promoted_fields={
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "chart_number": "sections.patient_information.chart_number",
        },
```

- [ ] **Step 3: Regenerate the compiled JSON artifacts**

Run: `just compile-schemas`
Expected: no errors printed; `git status` shows `data/form_schemas/ibv_form_standard_v2.json` and `data/form_schemas/disease_only_verification.json` modified.

- [ ] **Step 4: Add a regression test that both catalogs declare promoted_fields**

In `vera-backend/tests/unit/forms/test_schema_dsl.py`, add to `class TestCompiledArtifacts` (after `test_ibv_call_opening_and_key_terms`):

```python
    def test_ibv_promotes_the_full_column_set(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        assert doc.promoted_fields == {
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "chart_number": "sections.patient_information.chart_number",
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "member_policy_id": "sections.insurance_information.policy_number",
            "insurance_provider": "sections.insurance_reference_information.insurance_provider_name",
            "insurance_provider_phone_number": (
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        }

    def test_disease_only_promotes_only_patient_identity(self) -> None:
        doc = SCHEMAS["disease_only"][1]()
        assert doc.promoted_fields == {
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "chart_number": "sections.patient_information.chart_number",
        }
```

- [ ] **Step 5: Run the full DSL test file**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: all pass, including `test_committed_artifact_is_fresh` (proves the regenerated JSON matches a fresh compile) and the two new tests.

- [ ] **Step 6: Fix the now-stale comment referencing the removed `policy_id` handle**

In `vera-backend/tests/unit/forms/test_conditions.py`, the comment on the line asserting `"sections.insurance_information.policy_number"` is required currently reads:

```python
        # role=confirm, but it IS a system_fields target (member_id/policy_id)
        # and carries no default → still required at creation.
        assert "sections.insurance_information.policy_number" in fields
```

Update it to drop the removed handle name:

```python
        # role=confirm, but it IS a system_fields target (member_id)
        # and carries no default → still required at creation.
        assert "sections.insurance_information.policy_number" in fields
```

Run: `uv run pytest tests/unit/forms/test_conditions.py -v`
Expected: still passes (this test asserts on the path, not the handle name — `"member_id"` alone already covers it, per Step 1's `system_fields` edit).

- [ ] **Step 7: Run the full unit forms test directory**

Run: `uv run pytest tests/unit/forms/ -v`
Expected: all pass — this catches any other place a test asserted on the now-removed `"policy_id"` handle by name.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py \
        packages/vera_core/src/vera_core/forms/catalog/disease_only.py \
        data/form_schemas/ibv_form_standard_v2.json \
        data/form_schemas/disease_only_verification.json \
        tests/unit/forms/test_schema_dsl.py \
        tests/unit/forms/test_conditions.py
git commit -m "feat(forms): declare promoted_fields in both catalogs

ibv_standard promotes all 8 patient_form columns, reusing the paths
system_fields already has correct — this also fixes the intake-time bug
where insurance_provider/insurance_provider_phone_number used stale
field-key literals. disease_only promotes only the patient-identity
columns it actually collects.

Also drops the redundant policy_id system_fields handle from both
catalogs (pure duplicate of member_id, same path, no distinct consumer)
for coherence — member_id stays, since it drives the {{member_id}}
prompt spoken during the call."
```

---

### Task 4: Make `promote_columns` schema-driven and wire the intake call site

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/intake.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`upload_patient_form` only — `resolve_disputes` is Task 5)
- Modify: `vera-backend/scripts/seed_patient_data.py`
- Test: `vera-backend/tests/unit/forms/test_intake.py`
- Test: `vera-backend/tests/integration/control_plane/test_patient_forms_intake.py`

**Interfaces:**
- Consumes: `FormSchemaDoc.promoted_fields`, `PROMOTABLE_COLUMNS` (Task 2), catalog data (Task 3).
- Produces: `promote_columns(get_value: Callable[[str], Any], doc: FormSchemaDoc) -> PromotedIdentifiers` (replaces the old `promote_columns(payload: dict) -> PromotedIdentifiers`). `resolve_path(payload: dict[str, Any], path: str) -> Any` (renamed from `_resolve_path`, now public — Task 5 also needs it... actually Task 5 uses `current_values.get`, not `resolve_path`; `resolve_path` is only needed by intake call sites). `PromotedIdentifiers` loses its `member_id` field; every field now defaults to `None`.

- [ ] **Step 1: Write the failing unit tests**

Replace the entire `class TestPromoteColumns` in `vera-backend/tests/unit/forms/test_intake.py` (currently lines 249–300) with:

```python
def _doc_with_promoted_fields(promoted_fields: dict[str, str]) -> FormSchemaDoc:
    """A minimal v2 document whose system_fields (required for dsl.py validation)
    exactly mirror the given promoted_fields."""
    sections: dict[str, Any] = {}
    for path in promoted_fields.values():
        _, section_key, field_key = path.split(".")
        sections.setdefault(
            section_key,
            {"title": section_key, "role": "context", "fields": {}},
        )["fields"][field_key] = {"type": "text", "title": field_key, "role": "context"}
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "test_type",
            "system_fields": dict(promoted_fields),
            "promoted_fields": promoted_fields,
            "sections": sections,
            "tasks": [
                {"task_key": "main", "title": "Main", "sections": list(sections)},
            ],
        }
    )


_FULL_DOC = _doc_with_promoted_fields(
    {
        "patient_name": "sections.patient_information.patient_name",
        "patient_dob": "sections.patient_information.patient_dob",
        "chart_number": "sections.patient_information.chart_number",
        "appointment_date": "sections.appointment_information.appointment_date",
        "appointment_type": "sections.appointment_information.appointment_type",
        "member_policy_id": "sections.insurance_information.policy_number",
        "insurance_provider": "sections.insurance_reference_information.insurance_provider_name",
        "insurance_provider_phone_number": (
            "sections.insurance_reference_information.insurance_phone_number"
        ),
    }
)


class TestPromoteColumns:
    def test_maps_and_normalizes_from_a_nested_payload(self) -> None:
        payload = {
            "patient_information": {
                "patient_name": "  Jane Doe  ",
                "patient_dob": "1990-04-12",
                "chart_number": "  C-100 ",
            },
            "appointment_information": {
                "appointment_date": "2026-07-01",
                "appointment_type": "  New Patient ",
            },
            "insurance_information": {"policy_number": "  POL-42 "},
            "insurance_reference_information": {
                "insurance_provider_name": "  Blue Cross ",
                "insurance_phone_number": " +1 555 0100 ",
            },
        }
        promoted = promote_columns(lambda p: resolve_path(payload, p), _FULL_DOC)
        assert promoted.patient_name == "jane doe"
        assert promoted.chart_number == "C-100"
        assert promoted.patient_dob == date(1990, 4, 12)
        assert promoted.appointment_date == date(2026, 7, 1)
        assert promoted.appointment_type == "New Patient"
        assert promoted.member_policy_id == "POL-42"
        assert promoted.insurance_provider == "Blue Cross"
        assert promoted.insurance_provider_phone_number == "+1 555 0100"

    def test_maps_and_normalizes_from_a_flat_map(self) -> None:
        # The dispute-resolve shape: current_values keyed by root-anchored field_path.
        current_values = {
            "sections.patient_information.patient_name": "  Jane Doe  ",
            "sections.patient_information.patient_dob": "1990-04-12",
            "sections.insurance_reference_information.insurance_provider_name": "Blue Cross",
        }
        doc = _doc_with_promoted_fields(
            {
                "patient_name": "sections.patient_information.patient_name",
                "patient_dob": "sections.patient_information.patient_dob",
                "insurance_provider": (
                    "sections.insurance_reference_information.insurance_provider_name"
                ),
            }
        )
        promoted = promote_columns(current_values.get, doc)
        assert promoted.patient_name == "jane doe"
        assert promoted.patient_dob == date(1990, 4, 12)
        assert promoted.insurance_provider == "Blue Cross"

    def test_chart_number_na_becomes_none(self) -> None:
        doc = _doc_with_promoted_fields(
            {"chart_number": "sections.patient_information.chart_number"}
        )
        payload = {"patient_information": {"chart_number": "N/A"}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.chart_number is None

    def test_columns_the_schema_does_not_promote_stay_none(self) -> None:
        # disease_only-shaped: only 3 of the 8 promotable columns are declared.
        doc = _doc_with_promoted_fields(
            {"patient_name": "sections.patient_information.patient_name"}
        )
        promoted = promote_columns(lambda p: None, doc)
        assert promoted.patient_name is None  # get_value returned None too
        assert promoted.patient_dob is None
        assert promoted.appointment_date is None
        assert promoted.chart_number is None
        assert promoted.appointment_type is None
        assert promoted.member_policy_id is None
        assert promoted.insurance_provider is None
        assert promoted.insurance_provider_phone_number is None

    def test_bad_date_raises_with_the_schema_path(self) -> None:
        doc = _doc_with_promoted_fields(
            {"patient_dob": "sections.patient_information.patient_dob"}
        )
        payload = {"patient_information": {"patient_dob": "12/04/1990"}}
        with pytest.raises(InvalidIntakeValue) as exc:
            promote_columns(lambda p: resolve_path(payload, p), doc)
        assert exc.value.field_path == "sections.patient_information.patient_dob"
```

Add the two new imports this needs at the top of the file:

```python
from typing import Any

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import (
    InvalidIntakeValue,
    iter_leaf_answers,
    missing_required,
    promote_columns,
    required_intake_fields,
    resolve_path,
)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_intake.py -k TestPromoteColumns -v`
Expected: collection error / failures — `resolve_path` doesn't exist yet (still named `_resolve_path`) and `promote_columns` still takes a single `payload` argument.

- [ ] **Step 3: Rename `_resolve_path` to `resolve_path`**

In `vera-backend/packages/vera_core/src/vera_core/forms/intake.py`, rename the function (it's now called from outside the module too, by `patient_forms.py` in Step 6):

```python
def resolve_path(payload: dict[str, Any], path: str) -> Any:
    """Look up a root-anchored `sections.<key>...` path inside an intake payload
    nested by section key (the payload itself has no `sections` root — see
    `iter_leaf_answers`)."""
    node: Any = payload
    for part in path.removeprefix(PATH_PREFIX).split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node
```

Update its one internal caller, in `missing_required`:

```python
def missing_required(payload: dict[str, Any], schema_json: dict[str, Any]) -> list[str]:
    """Paths of every `required_intake_fields` target absent/blank in `payload`
    (root-anchored `sections.…` paths for v2 documents). Names only — never the
    values."""
    if is_v2(schema_json):
        return [
            path
            for path in required_intake_fields(schema_json)
            if _is_empty(resolve_path(payload, path))
        ]
    values = payload.get(_PATIENT_INFO)
    values = values if isinstance(values, dict) else {}
    return [
        f"{_PATIENT_INFO}.{field}"
        for field in required_intake_fields(schema_json)
        if _is_empty(values.get(field))
    ]
```

- [ ] **Step 4: Rewrite `PromotedIdentifiers` and `promote_columns`**

Replace the `PromotedIdentifiers` dataclass, `_get`, and `promote_columns` (the rest of the file — `_is_empty`, `required_intake_fields`, `resolve_path`, `missing_required`, `iter_leaf_answers`, `_clean_str`, `_parse_date` — is unchanged):

```python
@dataclass(frozen=True)
class PromotedIdentifiers:
    """The typed `patient_form` columns a schema's `promoted_fields` maps to — both the
    searchable identifiers and the worklist display fields. A schema that doesn't
    promote a given column (e.g. disease_only has no appointment/insurance sections)
    leaves that field at its `None` default."""

    patient_name: str | None = None
    patient_dob: date | None = None
    appointment_date: date | None = None
    chart_number: str | None = None
    appointment_type: str | None = None
    member_policy_id: str | None = None
    insurance_provider: str | None = None
    insurance_provider_phone_number: str | None = None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(value: Any, field_path: str) -> date | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidIntakeValue(field_path, "expected ISO date YYYY-MM-DD") from exc


def promote_columns(get_value: Callable[[str], Any], doc: FormSchemaDoc) -> PromotedIdentifiers:
    """Extract + normalize the `patient_form` columns `doc.promoted_fields` maps to
    (ADR §5 rule 3 — stable input for a future blind index). `get_value(path)` resolves
    one root-anchored schema path to its raw value — the caller supplies a nested-payload
    lookup at intake (`resolve_path`) or a flat `{field_path: value}` lookup at
    dispute-resolve (`dict.get`); both share the same schema-path namespace. Raises
    `InvalidIntakeValue` on a bad date."""
    values: dict[str, Any] = {}
    for column, path in (doc.promoted_fields or {}).items():
        raw = get_value(path)
        if column in ("patient_dob", "appointment_date"):
            values[column] = _parse_date(raw, path)
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

Delete the old `_get` helper (`def _get(payload, section, field): ...`) — it's no longer called by anything.

Update the module's imports: add `from collections.abc import Callable` (alongside the existing `from collections.abc import Iterator`), and change `from vera_core.forms.dsl import PATH_PREFIX, FormSchemaDoc` to keep `FormSchemaDoc` (already imported — no change needed there, it's already used by `required_intake_fields`).

Also delete the now-unused section-name constants that only `_get`/the old `promote_columns` consumed: `_PATIENT_INFO`, `_APPOINTMENT_INFO`, `_INSURANCE_INFO`, `_INSURANCE_REF` are STILL used by `missing_required`'s v1 fallback branch (`_PATIENT_INFO`) — keep `_PATIENT_INFO`. Delete `_APPOINTMENT_INFO`, `_INSURANCE_INFO`, `_INSURANCE_REF` (grep the file first to confirm no other caller — there is none once `_get`/old `promote_columns` are gone).

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_intake.py -v`
Expected: all tests pass (the rewritten `TestPromoteColumns` plus every untouched class above it — `TestRequiredIntakeFields`, `TestMissingRequired`, `TestRequiredIntakeFieldsV2`, `TestMissingRequiredV2`, `TestIterLeafAnswers`).

- [ ] **Step 6: Wire the intake call site**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`:

Add to the imports:
```python
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import (
    InvalidIntakeValue,
    iter_leaf_answers,
    missing_required,
    promote_columns,
    resolve_path,
)
```

Replace the promotion block in `upload_patient_form` (currently):
```python
        try:
            promoted = promote_columns(body.intake_payload)
        except InvalidIntakeValue as exc:
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="invalid field value",
                data={"fields": [exc.field_path]},
            ) from exc

        form = PatientForm(
            tenant_id=principal.tenant_id,
            schema_version_id=body.schema_version_id,
            status=FormStatus.READY_FOR_PROCESSING.value,
            intake_payload=body.intake_payload,
            patient_name=promoted.patient_name,
            patient_dob=promoted.patient_dob,
            appointment_date=promoted.appointment_date,
            chart_number=promoted.chart_number,
            member_id=promoted.member_id,
            appointment_type=promoted.appointment_type,
            member_policy_id=promoted.member_policy_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
            completion_pct=0,
            retry_count=0,
        )
```

with:
```python
        promoted = PromotedIdentifiers()
        if is_v2(version.schema_json):
            doc = FormSchemaDoc.model_validate(version.schema_json)
            try:
                promoted = promote_columns(
                    lambda p: resolve_path(body.intake_payload, p), doc
                )
            except InvalidIntakeValue as exc:
                raise CustomAPIException(
                    DefaultExceptionCode.VALIDATION_ERROR,
                    message="invalid field value",
                    data={"fields": [exc.field_path]},
                ) from exc

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
            member_policy_id=promoted.member_policy_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
            completion_pct=0,
            retry_count=0,
        )
```

(A v1 `schema_json` has no `promoted_fields`/`system_fields` DSL concept — `FormSchemaDoc.model_validate` would reject its shape outright, so promotion is skipped entirely for v1 and every promoted column stays `None`, matching how `completion_pct`/`completion_pct_v2` already branch on `is_v2` elsewhere in this file. `PromotedIdentifiers` needs importing too — add it to the `from vera_core.forms.intake import (...)` block above.)

- [ ] **Step 7: Wire the seed script call site**

In `vera-backend/scripts/seed_patient_data.py`, add `FormSchemaDoc` to its imports from `vera_core.forms.dsl`, and replace:

```python
        payload = _build_payload(schema_json, status)
        missing = missing_required(payload, schema_json)
        if missing:
            raise SystemExit(f"missing required patient_information fields: {missing}")
        promoted = promote_columns(payload)
```

with:

```python
        payload = _build_payload(schema_json, status)
        missing = missing_required(payload, schema_json)
        if missing:
            raise SystemExit(f"missing required patient_information fields: {missing}")
        doc = FormSchemaDoc.model_validate(schema_json)
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
```

and remove `member_id=promoted.member_id,` from the `PatientForm(...)` construction just below it (the rest of that call is unchanged):

```python
        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=version.id,
            status=status.value,
            intake_payload=payload,
            patient_name=promoted.patient_name,
            patient_dob=promoted.patient_dob,
            appointment_date=promoted.appointment_date,
            chart_number=promoted.chart_number,
            appointment_type=promoted.appointment_type,
            member_policy_id=promoted.member_policy_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
            completion_pct=0,
            retry_count=0,
        )
```

Add `resolve_path` to this script's `from vera_core.forms.intake import (...)` line too.

- [ ] **Step 8: Fix the integration test that masked the intake-time insurance bug**

In `vera-backend/tests/integration/control_plane/test_patient_forms_intake.py`, `test_upload_promotes_worklist_columns` currently overrides `insurance_reference_information` with legacy keys (`"insurance"`, `"phone_number"`) that only the OLD buggy `promote_columns` read — they aren't real schema fields. Replace:

```python
        "insurance_reference_information": {
            **INTAKE_PAYLOAD["insurance_reference_information"],
            "insurance": "Blue Cross",
            "phone_number": "+1 555 0100",
        },
```

with the real field names (overriding `INTAKE_PAYLOAD`'s base values so the assertion still proves promotion reads the override, not just the base payload):

```python
        "insurance_reference_information": {
            "insurance_provider_name": "Blue Cross",
            "insurance_phone_number": "+1 555 0100",
        },
```

- [ ] **Step 9: Run the intake integration tests**

Run: `uv run pytest tests/integration/control_plane/test_patient_forms_intake.py -v`
Expected: all pass, including `test_upload_promotes_worklist_columns` (now genuinely exercising the fixed insurance-field mapping) and `test_missing_required_returns_422_with_paths_no_phi` / `test_missing_required_field_outside_patient_information_returns_422` (unaffected by this task — still exercise `missing_required`, untouched).

- [ ] **Step 10: Type-check and lint the touched files**

Run:
```bash
uv run mypy packages/vera_core/src/vera_core/forms/intake.py \
             apps/control_plane/src/control_plane/api/v1/patient_forms.py \
             scripts/seed_patient_data.py
uv run ruff check packages/vera_core/src/vera_core/forms/intake.py \
                   apps/control_plane/src/control_plane/api/v1/patient_forms.py \
                   scripts/seed_patient_data.py
```
Expected: no errors.

- [ ] **Step 11: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/intake.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        scripts/seed_patient_data.py \
        tests/unit/forms/test_intake.py \
        tests/integration/control_plane/test_patient_forms_intake.py
git commit -m "feat(forms): make promote_columns schema-driven

promote_columns now reads doc.promoted_fields instead of hardcoded
section/field literals, taking a value-getter so the same function
serves a nested intake payload and a flat field_path map. Fixes the
intake-time bug where insurance_provider/insurance_provider_phone_number
used stale keys that never matched the real schema fields. member_id
dropped from PromotedIdentifiers (always None, no schema source)."
```

---

### Task 5: Wire promotion into dispute resolution

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`resolve_disputes`)
- Test: `vera-backend/tests/integration/control_plane/test_patient_forms_review.py`

**Interfaces:**
- Consumes: `promote_columns`, `FormSchemaDoc` (Task 4), `doc.promoted_fields` (Task 2/3).
- Produces: nothing new for other tasks — this is the feature's actual bug fix.

- [ ] **Step 1: Write the failing integration test**

In `vera-backend/tests/integration/control_plane/test_patient_forms_review.py`, add a fixture and two tests. Add this fixture after `_make_form_with_dispute` (reuses the same `sm`/tenant/schema pattern):

```python
INSURANCE_PROVIDER_NAME = "sections.insurance_reference_information.insurance_provider_name"


async def _make_form_with_promoted_field(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
) -> UUID:
    """A form whose current insurance_provider_name answer disagrees with the
    already-promoted patient_form.insurance_provider column — the bug this task fixes."""
    async with sm() as s, s.begin():
        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            status=FormStatus.EXCEPTION_REVIEW.value,
            intake_payload={"patient_information": {"patient_name": "Jane Doe"}},
            patient_name="jane doe",
            insurance_provider="Stale Provider",
            completion_pct=0,
            retry_count=0,
        )
        s.add(form)
        await s.flush()
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form.id,
                field_path=INSURANCE_PROVIDER_NAME,
                value={"value": "Stale Provider"},
                source=AnswerSource.INTAKE.value,
                is_current=True,
            )
        )
        return form.id


@pytest.fixture
async def promoted_field_form(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
    cleanup_forms: None,
) -> UUID:
    return await _make_form_with_promoted_field(
        admin_sessionmaker, tenant_id=rbac_world.tenant_id, schema_version_id=schema_version_id
    )
```

Then add these two tests in the `# ---- resolve ---` section of the file (find the existing dispute-resolve tests — the section testing `POST .../disputes:resolve` — and add alongside them):

```python
async def test_resolve_promotes_the_patient_form_column(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PROVIDER_NAME: "Corrected Provider"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider == "Corrected Provider"


async def test_resolve_leaves_promoted_columns_untouched_for_a_non_promoted_field(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_verification.patient_on_plan": "Yes"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider == "Stale Provider"  # unchanged
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/integration/control_plane/test_patient_forms_review.py -k promotes_the_patient_form_column -v`
Expected: `test_resolve_promotes_the_patient_form_column` fails — `form.insurance_provider` is still `"Stale Provider"` after the resolve call. (`test_resolve_leaves_promoted_columns_untouched...` already passes today since nothing writes to promoted columns at all yet — that's fine, it stays a regression guard going forward.)

- [ ] **Step 3: Wire the promotion into `resolve_disputes`**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`, in `resolve_disputes`, the function already builds `current_values` and looks up `version` right before computing `completion_pct`:

```python
    current_values: dict[str, Any] = {
        path: unwrap_value(value)
        for path, value in (
            await session.execute(
                select(FieldAnswer.field_path, FieldAnswer.value).where(
                    FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True)
                )
            )
        ).all()
    }
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    # v2 completion needs the values (applicable_when/required.when evaluate against
    # them); v1 only needs which paths are filled.
    form.completion_pct = (
        completion_pct_v2(current_values, version.schema_json)
        if is_v2(version.schema_json)
        else completion_pct(set(current_values), version.schema_json)
    )
```

Insert the promotion step right after `version` is loaded and before the `completion_pct` assignment:

```python
    current_values: dict[str, Any] = {
        path: unwrap_value(value)
        for path, value in (
            await session.execute(
                select(FieldAnswer.field_path, FieldAnswer.value).where(
                    FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True)
                )
            )
        ).all()
    }
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    # Re-derive promoted patient_form columns from the post-write current answers —
    # any resolve call that changes a promoted field's value (dispute or plain edit)
    # keeps the worklist columns in sync, not just intake (2026-07-10 design doc).
    if is_v2(version.schema_json):
        doc = FormSchemaDoc.model_validate(version.schema_json)
        promoted = promote_columns(current_values.get, doc)
        for column in doc.promoted_fields or {}:
            new_value = getattr(promoted, column)
            if getattr(form, column) != new_value:
                setattr(form, column, new_value)
    # v2 completion needs the values (applicable_when/required.when evaluate against
    # them); v1 only needs which paths are filled.
    form.completion_pct = (
        completion_pct_v2(current_values, version.schema_json)
        if is_v2(version.schema_json)
        else completion_pct(set(current_values), version.schema_json)
    )
```

`FormSchemaDoc` and `promote_columns` are already imported from Task 4's Step 6.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_patient_forms_review.py -v`
Expected: all tests in the file pass, including the two new ones and every pre-existing dispute-resolve/list/detail test.

- [ ] **Step 5: Type-check and lint**

Run: `uv run mypy apps/control_plane/src/control_plane/api/v1/patient_forms.py && uv run ruff check apps/control_plane/src/control_plane/api/v1/patient_forms.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/integration/control_plane/test_patient_forms_review.py
git commit -m "fix(api): resolve_disputes promotes patient_form columns

The endpoint wrote field_answer rows but never re-derived the promoted
patient_form columns (patient_name, insurance_provider, etc.), leaving
worklist search/display stale after a dispute correction. Now recomputes
promoted columns from the post-write current answers on every resolve
call that touches a promoted field, using the same schema-driven mapping
intake uses."
```

---

### Task 6: Drop the dead `member_id` column

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/patient_form.py`
- Create: a new alembic migration (path assigned by `just makemigration`)
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`PatientFormDetail`, `_build_detail`)
- Modify: `vera-frontend/src/lib/patient-forms/types.ts`

**Interfaces:** None — purely subtractive, no new interface. `PatientForm.member_id`, `PatientFormDetail.member_id` cease to exist.

- [ ] **Step 1: Remove the column from the model**

In `vera-backend/packages/vera_core/src/vera_core/models/patient_form.py`, delete this line (it's the only line in the model referencing `member_id`):

```python
    member_id: Mapped[str | None] = mapped_column(String(128), nullable=True, info=PHI_INFO)
```

- [ ] **Step 2: Remove `member_id` from the API response**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`, remove the field from `PatientFormDetail`:

```python
class PatientFormDetail(BaseModel):
    id: UUID
    status: str
    insurance_type: str
    schema_version_id: UUID
    completion_pct: float
    created_at: datetime
    updated_at: datetime
    patient_name: str | None
    chart_number: str | None
    appointment_date: date | None
    # Voice-lab-style toggle stored on the form (default True) — the UI's re-queue
    # toggle pre-loads from here so an operator's earlier choice round-trips.
    ivr_navigation_enabled: bool
    fields: list[FieldView]
```

(`member_id: str | None` is deleted.) And remove it from `_build_detail`'s construction:

```python
    return PatientFormDetail(
        id=form.id,
        status=form.status,
        insurance_type=form_schema.insurance_type,
        schema_version_id=form.schema_version_id,
        completion_pct=float(form.completion_pct),
        created_at=form.created_at,
        updated_at=form.updated_at,
        patient_name=form.patient_name,
        chart_number=form.chart_number,
        appointment_date=form.appointment_date,
        ivr_navigation_enabled=form.ivr_navigation_enabled,
        fields=[FieldView(**view) for view in views],
    )
```

(`member_id=form.member_id,` is deleted.)

- [ ] **Step 3: Generate the migration**

Run: `just makemigration "drop_dead_patient_form_member_id_column"`
Expected: a new file appears under `vera-backend/migrations/versions/`, printed by the command, with `down_revision` auto-set to the current single head (`8115d1763daf`, from Task 1) and an autogenerated body (likely `op.drop_column("patient_form", "member_id")` plus possibly unrelated drift ops from other model/DB differences — see the CLAUDE.md note that autogenerate also emits stray index-drop ops from known drift; if any appear, delete them and keep only the `member_id` drop).

- [ ] **Step 4: Make the migration idempotent**

Edit the generated file's `upgrade`/`downgrade` bodies to match this repo's dual-path migration rule (mirrors `20260709_1834_8115d1763daf_...`'s pattern):

```python
def upgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS member_id")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS member_id varchar(128)"
    )
```

Leave the file's `revision`/`down_revision`/docstring/`Revision ID`/`Create Date` exactly as generated.

- [ ] **Step 5: Apply the migration**

Run: `just migrate`
Expected: completes with no errors; last line references the new revision.

- [ ] **Step 6: Remove the frontend type field**

In `vera-frontend/src/lib/patient-forms/types.ts`, remove this line from `PatientFormDetail`:

```typescript
  member_id: string | null
```

- [ ] **Step 7: Run the backend test suite**

Run: `uv run pytest -q`
Expected: all tests pass (this column was never read anywhere, so no other test should reference it — confirm no failures mention `member_id`).

- [ ] **Step 8: Type-check and lint everything touched**

Run (from `vera-backend/`):
```bash
uv run mypy
uv run ruff check .
```
Run (from `vera-frontend/`):
```bash
npm run build
npm run lint
```
Expected: no errors in either.

- [ ] **Step 9: Commit (backend)**

```bash
cd vera-backend
git add packages/vera_core/src/vera_core/models/patient_form.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        migrations/versions/
git commit -m "fix(patient-forms): drop the dead member_id column

Every code path that touched it set it to None — the only live
identifier-shaped column is member_policy_id (sourced from
insurance_information.policy_number). Not the same concept as the
schema's system_fields[\"member_id\"] handle, which stays untouched
(it drives the {{member_id}} prompt spoken during the call)."
```

- [ ] **Step 10: Commit (frontend)**

```bash
cd ../vera-frontend
git add src/lib/patient-forms/types.ts
git commit -m "chore(patient-forms): drop unused member_id field from the detail type

Backend response no longer includes it; nothing in the frontend read
it (no .member_id usage anywhere)."
```

---

### Task 7: Simplify pass and final verification

**Files:** whichever files the simplifier touches (review its diff before accepting).

- [ ] **Step 1: Run the code-simplifier**

Per the repo-root `CLAUDE.md`, run the `code-simplifier` agent ("simplify code") over everything changed in Tasks 1–6, in this same session. It should not change behavior — only clarity/consistency.

- [ ] **Step 2: Re-run the backend gate**

Run (from `vera-backend/`): `just check`
Expected: `lint` (ruff format --check + ruff check), `typecheck` (mypy --strict), and `test` (pytest) all pass.

- [ ] **Step 3: Re-run the frontend gate**

Run (from `vera-frontend/`):
```bash
npm run build
npm run lint
npm test
```
Expected: all pass.

- [ ] **Step 4: Manually verify the end-to-end fix**

With `just up` running and the API up (`just api`), boot a quick sanity check: seed a form via `just test_seed_patient_data exception_review`, hit `POST /api/v1/patient-forms/{id}/disputes:resolve` with a change to `sections.insurance_reference_information.insurance_provider_name`, then `GET /api/v1/patient-forms` and confirm the worklist row's `insurance_provider` reflects the new value. This is the exact bug from the original report — confirm it's gone.

- [ ] **Step 5: Commit any simplifier fixups**

If the simplifier changed anything beyond what Tasks 1–6 already committed:
```bash
git add -A
git commit -m "refactor: simplification pass over dispute-resolve promotion changes"
```
(Skip this step entirely if the simplifier made no changes.)
