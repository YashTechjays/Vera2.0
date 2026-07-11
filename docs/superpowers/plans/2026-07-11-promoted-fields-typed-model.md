# Typed PromotedFields DSL Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the free-form `promoted_fields: dict[str, str] | None` on `FormSchemaDoc` with a required, fully-typed `PromotedFields` pydantic model whose attributes mirror `PatientForm`'s promoted columns, upgrade `disease_only` to map all eight columns, and add a timestamp-gated cleanup migration for dev forms pinned to now-invalid documents.

**Architecture:** One pydantic model (`PromotedFields`) becomes both the authoring grammar and the compiled-artifact contract for the promoted-columns block. All eight attributes are required `str` leaf paths — omitting any is a `ValidationError` at authoring/compile/load. Consumers (`intake.promote_columns`, dispute-resolve in `patient_forms.py`) iterate a `items()` helper instead of `or {}`-guarded dicts. A parity test pins `PromotedFields` ↔ `PromotedIdentifiers` ↔ `PatientForm` columns together.

**Tech Stack:** Python 3.12, pydantic v2, SQLAlchemy/alembic, pytest, `just` recipes (uv-backed).

**Spec:** `docs/superpowers/specs/2026-07-11-promoted-fields-typed-model-design.md`

## Global Constraints

- All work is in `vera-backend/`; run commands from that directory. The frontend is untouched.
- **Never hand-edit `data/form_schemas/*.json`** — change catalog modules, then `just compile-schemas`.
- `ibv_form_standard_v2.json` must stay **byte-identical** after recompile (`git diff` empty for that file). `disease_only_verification.json` changes.
- `PromotedFields` attribute declaration order must be exactly: `patient_name, patient_dob, chart_number, appointment_date, appointment_type, member_id, insurance_provider, insurance_provider_phone_number` (the current ibv artifact key order — this is what keeps ibv byte-identical).
- Code style: PEP 695, ruff + mypy --strict clean (`just check` = lint + typecheck + test).
- Migration revision IDs are alembic's random hex via `just makemigration` — never hand-numbered.
- PHI rules: never log field values; the migration deletes PHI rows — the migration docstring must not embed example values.
- Repo rule: after implementation, run the **code-simplifier** agent, then re-run `just check`, before claiming done (Task 5).

---

### Task 1: `PromotedFields` model, validator rewire, catalog upgrades, artifacts, consumers

This is one atomic unit — the required-field change cannot be staged green file-by-file (catalogs, artifacts, and both consumers all break the moment the model changes), so tests are updated first, then every producer/consumer flips in a single commit.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py` (delete `PROMOTABLE_COLUMNS` ~line 56-69; add `PromotedFields` before `FormSchemaDoc` ~line 414; change `FormSchemaDoc.promoted_fields` ~line 430; rewire validator ~line 663-679)
- Modify: `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` (imports ~line 26-47; `promoted_fields` ~line 1033-1047)
- Modify: `packages/vera_core/src/vera_core/forms/catalog/disease_only.py` (imports ~line 23-39; `_context_sections` ~line 70; sections/system_fields/promoted_fields/tasks in `build_disease_only` ~line 340-421)
- Modify: `packages/vera_core/src/vera_core/forms/intake.py` (`promote_columns` ~line 165-188, `PromotedIdentifiers` docstring ~line 122-127)
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py:676`
- Modify: `tests/unit/forms/test_schema_dsl.py` (fixture ~line 22-57; promoted tests ~line 106-130 and 354-380)
- Modify: `tests/unit/forms/test_intake.py` (helpers ~line 252-292, ~line 375-402; `test_columns_the_schema_does_not_promote_stay_none` ~line 352)
- Regenerate: `data/form_schemas/ibv_form_standard_v2.json` (no diff expected), `data/form_schemas/disease_only_verification.json` (diff expected)

**Interfaces:**
- Produces: `class PromotedFields(_Model)` with the eight required `str` attributes (order per Global Constraints) and `def items(self) -> list[tuple[str, str]]` returning `(column, path)` pairs in declaration order. `FormSchemaDoc.promoted_fields: PromotedFields` (required). Later tasks import `PromotedFields` from `vera_core.forms.dsl`.

- [ ] **Step 1: Update `tests/unit/forms/test_schema_dsl.py` (failing tests first)**

Add the import and a module-level canonical-columns tuple near the top (after the existing imports):

```python
from vera_core.forms.dsl import (
    FormSchemaDoc,
    PromotedFields,
    Validation,
    compile_document,
    load_document,
    parse_date_format,
)

# The eight patient_form columns every schema must promote, in artifact key order.
PROMOTED_COLUMNS: tuple[str, ...] = (
    "patient_name",
    "patient_dob",
    "chart_number",
    "appointment_date",
    "appointment_type",
    "member_id",
    "insurance_provider",
    "insurance_provider_phone_number",
)
```

In `minimal_doc`, add two keys to the `doc` dict right after `"insurance_type"` (all eight columns may legally share one leaf; the fixture stays minimal):

```python
        "system_fields": {"plan_type": "sections.basics.plan_type"},
        "promoted_fields": {c: "sections.basics.plan_type" for c in PROMOTED_COLUMNS},
```

Replace `test_ibv_promotes_the_full_column_set` (line 106) — same mapping, now compared as the model:

```python
    def test_ibv_promotes_the_full_column_set(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        assert doc.promoted_fields == PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.insurance_information.policy_number",
            insurance_provider=(
                "sections.insurance_reference_information.insurance_provider_name"
            ),
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        )
```

Replace `test_disease_only_promotes_identity_and_member_id` (line 123):

```python
    def test_disease_only_promotes_the_full_column_set(self) -> None:
        doc = SCHEMAS["disease_only"][1]()
        assert doc.promoted_fields == PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.policy_details.policy_number",
            insurance_provider=(
                "sections.insurance_reference_information.insurance_provider_name"
            ),
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        )
```

Replace the four promoted-fields validation tests (lines 354-380) with:

```python
    def test_promoted_fields_block_is_required(self) -> None:
        doc = minimal_doc()
        del doc["promoted_fields"]
        with pytest.raises(ValidationError, match="Field required"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_every_column_is_required(self) -> None:
        doc = minimal_doc()
        del doc["promoted_fields"]["member_id"]
        with pytest.raises(ValidationError, match="Field required"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_unknown_column(self) -> None:
        doc = minimal_doc()
        doc["promoted_fields"]["not_a_column"] = "sections.basics.plan_type"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_path_not_a_leaf(self) -> None:
        doc = minimal_doc()
        doc["promoted_fields"]["patient_name"] = "sections.basics.missing"
        with pytest.raises(ValidationError, match="does not resolve to a leaf"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_path_not_backed_by_system_fields(self) -> None:
        # sections.basics.notes is a real leaf but not a system_fields target.
        doc = minimal_doc()
        doc["promoted_fields"]["patient_name"] = "sections.basics.notes"
        with pytest.raises(ValidationError, match="not a system_fields target"):
            FormSchemaDoc.model_validate(doc)
```

- [ ] **Step 2: Run the DSL tests to verify they fail**

Run (from `vera-backend/`): `uv run pytest tests/unit/forms/test_schema_dsl.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'PromotedFields'`.

- [ ] **Step 3: Implement `PromotedFields` in `dsl.py`**

Delete the `PROMOTABLE_COLUMNS` frozenset (lines 56-69, including its comment). Add the model in the "Document" section, immediately above `class FormSchemaDoc` (after `class Contradiction`):

```python
class PromotedFields(_Model):
    """patient_form column -> root-anchored leaf path.

    The attribute set mirrors PatientForm's promoted columns (searchable
    identifiers + worklist display fields); every schema must map all of them,
    so a new schema can neither forget nor typo a column (enforced at
    authoring/compile/load — extra="forbid", no defaults). Declaration order
    is the compiled-artifact key order. Consumed by
    vera_core.forms.intake.promote_columns and the dispute-resolve promotion
    in control_plane.api.v1.patient_forms.
    """

    patient_name: str
    patient_dob: str
    chart_number: str
    appointment_date: str
    appointment_type: str
    member_id: str
    insurance_provider: str
    insurance_provider_phone_number: str

    def items(self) -> list[tuple[str, str]]:
        """(column, leaf path) pairs, in declaration order."""
        return [(column, getattr(self, column)) for column in type(self).model_fields]
```

In `FormSchemaDoc`, replace the `promoted_fields` field and its comment (lines 424-430):

```python
    # patient_form column name -> root-anchored leaf path. Required: every schema
    # must map every promotable column (PromotedFields). Each path must also be a
    # system_fields target (validated below) — that guarantees a promoted column can
    # never be *unexpectedly* empty at intake (system_fields targets are exactly what
    # required_intake_fields enforces at creation, intake.py), though a leaf with its
    # own `default` is still allowed to be absent from the payload (it counts as
    # filled either way).
    promoted_fields: PromotedFields
```

In `_validate_document`, replace the promoted-fields loop (lines 663-679) with:

```python
        # promoted fields — patient_form columns re-derived from the current answer at
        # dispute-resolve time too (not just intake). Column names are enforced by the
        # PromotedFields model itself; each path must be a system_fields target so a
        # promoted column is never legitimately empty.
        system_field_paths = set((self.system_fields or {}).values())
        for column, path in self.promoted_fields.items():
            if path not in leaves:
                errors.append(f"promoted_fields.{column}: {path!r} does not resolve to a leaf")
            elif path not in system_field_paths:
                errors.append(
                    f"promoted_fields.{column}: {path!r} is not a system_fields target "
                    "(promoted fields must be guaranteed present at intake)"
                )
```

- [ ] **Step 4: Switch `ibv_standard.py` to the typed model**

Add `PromotedFields` to the `vera_core.forms.dsl` import block (alphabetical: between `Leaf` and `RequiredWhen` — actual order in the block: after `Group`/`Leaf`, keep ruff's isort happy; `just fmt` will settle it). Replace the `promoted_fields={...}` dict (lines 1036-1047) with:

```python
        # patient_form columns re-derived from the current answer at intake AND
        # dispute-resolve (2026-07-10 design doc). Every path must also be a
        # system_fields target (dsl.py validates this).
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.insurance_information.policy_number",
            insurance_provider=(
                "sections.insurance_reference_information.insurance_provider_name"
            ),
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
```

- [ ] **Step 5: Upgrade `disease_only.py` to map all eight columns**

Add `PromotedFields` to its `vera_core.forms.dsl` import block. In `_context_sections()` (line 70), add a new entry after `"patient_information"` (mirrors ibv_standard's appointment section):

```python
        "appointment_information": Section(
            title="Appointment Information",
            role="context",
            description=(
                "Upcoming appointment details supplied at intake; background for the agent."
            ),
            fields={
                "appointment_type": Leaf(
                    type="enum",
                    title="Appointment Type",
                    role="context",
                    values=["New Patient", "Reverification", "Follow Up Visit", "N/A"],
                    default="N/A",
                    required=True,
                ),
                "appointment_date": Leaf(
                    type="date",
                    title="Appointment Date",
                    role="context",
                    required=True,
                    validation=DATE_VALIDATION,
                ),
            },
        ),
```

In `build_disease_only()`:

1. Add a collect section to the `sections` dict, after `"exclusions_limitations"` (mirrors ibv_standard's insurance reference section, trimmed to the two promoted leaves):

```python
            "insurance_reference_information": Section(
                title="Insurance Reference Information",
                description=(
                    "Reference details about the insurance provider, collected when available."
                ),
                fields={
                    "insurance_provider_name": text_ask(
                        "Insurance Provider Name",
                        "Could you provide the full name of the insurance provider?",
                    ),
                    "insurance_phone_number": text_ask(
                        "Insurance Provider Phone",
                        "What is the primary phone number for the insurance provider?",
                        type_="phone",
                    ),
                },
            ),
```

2. Assign it to the `wrap_up` task (every collect section needs exactly one task): change that task's `sections=["representative_details"]` to `sections=["insurance_reference_information", "representative_details"]`.

3. Extend `system_fields` (after the `"member_id"` entry — promoted paths must be system_fields targets; handles match ibv_standard's):

```python
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "insurance_provider_name": (
                "sections.insurance_reference_information.insurance_provider_name"
            ),
            "insurance_provider_phone_number": (
                "sections.insurance_reference_information.insurance_phone_number"
            ),
```

4. Replace the `promoted_fields={...}` dict and its now-stale comment (lines 359-367) with:

```python
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.policy_details.policy_number",
            insurance_provider=(
                "sections.insurance_reference_information.insurance_provider_name"
            ),
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
```

Intake consequence (by design, spec §2): `appointment_date`, `insurance_provider_name`, `insurance_phone_number` become required intake fields for this schema (`appointment_type` has a default, so not).

- [ ] **Step 6: Update the two consumers**

`packages/vera_core/src/vera_core/forms/intake.py` — in `promote_columns` (line 174), change:

```python
    for column, path in (doc.promoted_fields or {}).items():
```

to:

```python
    for column, path in doc.promoted_fields.items():
```

Update the `PromotedIdentifiers` docstring (lines 124-127) — the "schema that doesn't promote a given column" sentence is now wrong:

```python
    """The typed `patient_form` columns a schema's `promoted_fields` maps to — both the
    searchable identifiers and the worklist display fields. Every schema maps every
    column (PromotedFields is total), but a mapped value can still come back `None`
    (payload omitted a defaulted leaf; chart_number's "N/A" normalization)."""
```

`apps/control_plane/src/control_plane/api/v1/patient_forms.py` line 676, change:

```python
        for column in doc.promoted_fields or {}:
```

to:

```python
        for column, _path in doc.promoted_fields.items():
```

- [ ] **Step 7: Update `tests/unit/forms/test_intake.py` fixtures**

Replace `_doc_with_promoted_fields` and `_FULL_DOC` (lines 252-292) with a merge-based helper (promoted_fields is now total, so partial fixtures pad with canonical paths; sections are still derived from whatever the merged map references):

```python
_CANONICAL_PROMOTED: dict[str, str] = {
    "patient_name": "sections.patient_information.patient_name",
    "patient_dob": "sections.patient_information.patient_dob",
    "chart_number": "sections.patient_information.chart_number",
    "appointment_date": "sections.appointment_information.appointment_date",
    "appointment_type": "sections.appointment_information.appointment_type",
    "member_id": "sections.insurance_information.policy_number",
    "insurance_provider": "sections.insurance_reference_information.insurance_provider_name",
    "insurance_provider_phone_number": (
        "sections.insurance_reference_information.insurance_phone_number"
    ),
}


def _doc_with_promoted_fields(overrides: dict[str, str] | None = None) -> FormSchemaDoc:
    """A minimal v2 document promoting all eight columns (PromotedFields is total).
    `overrides` repoints individual columns; system_fields (required for dsl.py
    validation) exactly mirror the merged map, and every referenced path gets a
    context text leaf."""
    promoted_fields = {**_CANONICAL_PROMOTED, **(overrides or {})}
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
            # All fixture sections are role="context" (no voice collection needed for
            # these tests), so none may be assigned to a task (dsl.py: "only collect
            # sections belong to tasks") — an empty task list is the valid v2 shape
            # for a document with zero collect sections.
            "tasks": [],
        }
    )


_FULL_DOC = _doc_with_promoted_fields()
```

Update the callers that passed subsets (the merged doc makes the payload, not the doc, decide which values exist):

- `test_maps_and_normalizes_from_a_flat_map` (line 330): `doc = _doc_with_promoted_fields()` (drop the 3-entry dict — the flat `current_values` map already only has 3 paths; the other five columns resolve to `None`). Keep the three assertions.
- `test_chart_number_na_becomes_none` (line 345): `doc = _doc_with_promoted_fields()`.
- `test_columns_the_schema_does_not_promote_stay_none` (line 352): rename to `test_absent_payload_values_promote_to_none`, body becomes:

```python
    def test_absent_payload_values_promote_to_none(self) -> None:
        # Every column is mapped, but a payload can still lack the values.
        promoted = promote_columns(lambda p: None, _FULL_DOC)
        assert promoted.patient_name is None
        assert promoted.patient_dob is None
        assert promoted.appointment_date is None
        assert promoted.chart_number is None
        assert promoted.appointment_type is None
        assert promoted.member_id is None
        assert promoted.insurance_provider is None
        assert promoted.insurance_provider_phone_number is None
```

- `test_bad_date_raises_with_the_schema_path` (line 368): `doc = _doc_with_promoted_fields()` (canonical map already routes `patient_dob` to that path).

Replace `_doc_with_date_format` (line 375): keep its bespoke date-typed leaf, pad the other seven columns onto a filler leaf:

```python
def _doc_with_date_format(date_format: str) -> FormSchemaDoc:
    """A doc whose patient_dob leaf declares `validation.date_format`, mirroring
    ibv_standard.py's real `DATE_VALIDATION = Validation(date_format="M/D/YYYY")`
    — the review UI prompts for and submits values in exactly this format. The
    other seven (now-mandatory) columns share a filler leaf the tests never set."""
    dob_path = "sections.patient_information.patient_dob"
    filler_path = "sections.patient_information.filler"
    promoted_fields = {
        column: (dob_path if column == "patient_dob" else filler_path)
        for column in _CANONICAL_PROMOTED
    }
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "test_type",
            "system_fields": {"patient_dob": dob_path, "filler": filler_path},
            "promoted_fields": promoted_fields,
            "sections": {
                "patient_information": {
                    "title": "Patient Information",
                    "role": "context",
                    "fields": {
                        "patient_dob": {
                            "type": "date",
                            "title": "Patient DOB",
                            "role": "context",
                            "validation": {"date_format": date_format},
                        },
                        "filler": {"type": "text", "title": "Filler", "role": "context"},
                    },
                },
            },
            "tasks": [],
        }
    )
```

(Note: `appointment_date` maps to the text filler leaf here; `promote_columns` date-parses by **column name**, but the tests never supply a filler value, so it resolves to `None` — no parse attempted.)

- [ ] **Step 8: Recompile artifacts and verify the ibv artifact did not move**

Run: `just compile-schemas`
Then: `git diff --stat data/form_schemas/`
Expected: `disease_only_verification.json` changed; `ibv_form_standard_v2.json` NOT in the diff. If ibv moved, the `PromotedFields` declaration order is wrong — fix the model, recompile.

- [ ] **Step 9: Run the forms unit tests**

Run: `uv run pytest tests/unit/forms/ -q`
Expected: PASS (all).

- [ ] **Step 10: Run the full gate**

Run: `just check`
Expected: ruff + mypy --strict + pytest all green. Integration tests skip without a running DB — that's fine; if `just up` infra is already running, they must pass too.

- [ ] **Step 11: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py \
        packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py \
        packages/vera_core/src/vera_core/forms/catalog/disease_only.py \
        packages/vera_core/src/vera_core/forms/intake.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/unit/forms/test_schema_dsl.py tests/unit/forms/test_intake.py \
        data/form_schemas/disease_only_verification.json
git commit -m "feat(forms): promoted_fields is a required typed PromotedFields model"
```

---

### Task 2: Three-way parity test (PromotedFields ↔ PromotedIdentifiers ↔ PatientForm)

**Files:**
- Modify: `tests/unit/forms/test_schema_dsl.py` (append a new test class at the end)

**Interfaces:**
- Consumes: `PromotedFields` (Task 1), `vera_core.forms.intake.PromotedIdentifiers`, `vera_core.models.patient_form.PatientForm` (SQLAlchemy model — `__table__` introspection needs no DB).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/forms/test_schema_dsl.py`:

```python
class TestPromotedColumnParity:
    """PromotedFields (DSL contract), PromotedIdentifiers (intake value carrier) and
    PatientForm (the table) must agree on the promoted column set — a future column
    add that misses one of the three fails here, not in production."""

    # The documented contract: PatientForm's promoted searchable-identifier +
    # worklist-display columns. PatientForm has many non-promoted columns, so
    # this literal — not introspection — defines "promoted".
    EXPECTED = frozenset(PROMOTED_COLUMNS)

    def test_dsl_model_matches_the_contract(self) -> None:
        assert set(PromotedFields.model_fields) == self.EXPECTED

    def test_intake_dataclass_matches_the_contract(self) -> None:
        from dataclasses import fields as dataclass_fields

        from vera_core.forms.intake import PromotedIdentifiers

        assert {f.name for f in dataclass_fields(PromotedIdentifiers)} == self.EXPECTED

    def test_patient_form_table_has_every_promoted_column(self) -> None:
        from vera_core.models.patient_form import PatientForm

        assert self.EXPECTED <= {c.name for c in PatientForm.__table__.columns}
```

- [ ] **Step 2: Run to verify it passes (parity already holds after Task 1)**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py::TestPromotedColumnParity -q`
Expected: PASS. Sanity-check the test bites: temporarily remove `"member_id"` from `PROMOTED_COLUMNS`, rerun, expect 2 failures, restore.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/forms/test_schema_dsl.py
git commit -m "test(forms): pin PromotedFields/PromotedIdentifiers/PatientForm column parity"
```

---

### Task 3: Timestamp-gated cleanup data migration

Removes forms pinned to `dsl_version 2.x` documents whose `promoted_fields` block is incomplete (all pre-this-change v2 forms). Gated twice: the predicate can't match documents produced after this change (validation forbids them), and a hard `created_at < 2026-07-31` cutoff means no far-future row can ever be deleted even if that assumption breaks (a few weeks of headroom, because dev keeps creating forms pinned to the still-published block-less document until this branch deploys). Old `schema_version` rows stay (nothing loads an unpinned version; deleting them would trip `prompt_version`'s RESTRICT FK).

**Files:**
- Create: `migrations/versions/<generated>_delete_forms_pinned_to_pre_promoted_fields_docs.py` (via `just makemigration`)

**Interfaces:**
- Consumes: tables `patient_form` (FK `schema_version_id`), `schema_version` (`schema_json` is `JSON` — cast to `jsonb`), `call` + `export_artifact` (both `form_id` RESTRICT → deleted explicitly first; everything under `call` CASCADEs: `call_lineage`, `call_event`, `transcript`, `intervention_event`, `human_rating`, `call_provider_usage`), `field_answer` (CASCADE from `patient_form`).

- [ ] **Step 1: Generate the revision skeleton**

Run: `just makemigration message="delete forms pinned to pre promoted_fields docs"`
Expected: a new date-prefixed file under `migrations/versions/` with a random hex revision id. ⚠ Autogenerate emits unrelated drift ops (`ix_audit_log_tenant_seq`, `ix_auth_audit_log_*` index drops) — delete everything inside `upgrade()`/`downgrade()`; keep only the revision header.

- [ ] **Step 2: Write the migration body**

Replace the file body below the revision header with (keep the generated `revision`/`down_revision` values):

```python
"""delete forms pinned to pre promoted_fields docs

One-time, timestamp-gated destructive cleanup (2026-07-11 design doc).
`promoted_fields` became a REQUIRED eight-key block on every dsl 2.x document;
forms pinned (RESTRICT FK) to older v2 schema_version rows can no longer parse
and cannot be backfilled (the old documents lack the leaves the new columns
must reference), so their pre-prod test forms are removed.

Two independent guards, so this can never eat future data:
- predicate: the pinned document is dsl 2.x AND its promoted_fields block is
  missing at least one required key — impossible for any document compiled
  after this change (dsl.py validation rejects it at authoring/compile/load);
- cutoff: only rows created before 2026-07-31 UTC qualify — headroom past
  the planned deploy (dev keeps creating forms pinned to the block-less
  document until then), while still guaranteeing a worst-case future
  (validation loosened, predicate bug) touches nothing created after July 2026.

Delete order honors the RESTRICT FKs: export_artifact and call first
(everything under call CASCADEs), then patient_form (field_answer CASCADEs).
Stale schema_version rows stay — nothing loads a version no form pins, and
prompt_version references them RESTRICT.

Runs on the privileged migration connection (BYPASSRLS) like every migration;
the affected tables are FORCE RLS, so an RLS-bound role would silently match
zero rows instead.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "<KEEP GENERATED>"
down_revision: str | None = "<KEEP GENERATED>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The eight keys PromotedFields requires (vera_core/forms/dsl.py).
_REQUIRED_KEYS = (
    "patient_name",
    "patient_dob",
    "chart_number",
    "appointment_date",
    "appointment_type",
    "member_id",
    "insurance_provider",
    "insurance_provider_phone_number",
)
_KEYS_SQL = ", ".join(f"'{k}'" for k in _REQUIRED_KEYS)
_CUTOFF = "2026-07-31 00:00:00+00"

_STALE_FORMS = f"""
    SELECT pf.id
    FROM patient_form pf
    JOIN schema_version sv ON sv.id = pf.schema_version_id
    WHERE pf.created_at < TIMESTAMPTZ '{_CUTOFF}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT COALESCE(
          jsonb_exists_all(
              (sv.schema_json::jsonb) -> 'promoted_fields', ARRAY[{_KEYS_SQL}]
          ),
          FALSE
      )
"""


def upgrade() -> None:
    op.execute(f"DELETE FROM export_artifact WHERE form_id IN ({_STALE_FORMS})")
    op.execute(f"DELETE FROM call WHERE form_id IN ({_STALE_FORMS})")
    op.execute(f"DELETE FROM patient_form WHERE id IN ({_STALE_FORMS})")


def downgrade() -> None:
    # Data deletion is irreversible; the removed rows were pre-prod test forms.
    pass
```

(`jsonb_exists_all` is the function form of the `?&` operator — used so no driver mistakes `?` for a bind-parameter marker. A missing/`null` `promoted_fields` makes it return `NULL`; `NOT COALESCE(..., FALSE)` turns that into "stale".)

- [ ] **Step 3: Exercise the migration against the local DB**

Requires local infra: `just up` (postgres on localhost:5432, user/pass/db `vera`).

Count candidates before migrating:

```bash
docker compose exec -T postgres psql -U vera -d vera -c "
  SELECT count(*) FROM patient_form pf
  JOIN schema_version sv ON sv.id = pf.schema_version_id
  WHERE (sv.schema_json ->> 'dsl_version') LIKE '2.%'
    AND NOT COALESCE(jsonb_exists_all((sv.schema_json::jsonb) -> 'promoted_fields',
      ARRAY['patient_name','patient_dob','chart_number','appointment_date',
            'appointment_type','member_id','insurance_provider',
            'insurance_provider_phone_number']), FALSE);"
```

Run: `just migrate`
Expected: `alembic upgrade head` succeeds. Re-run the count: `0`. Total `patient_form` count only dropped by the candidate number (v1 + fresh v2 forms survive).

- [ ] **Step 4: Verify a fresh-DB migration chain still works (CI parity)**

CI runs `alembic upgrade head` from scratch on an empty Postgres; the deletes match zero rows there. Locally approximate with a throwaway DB:

```bash
docker compose exec -T postgres psql -U vera -d postgres -c "DROP DATABASE IF EXISTS vera_migration_smoke;"
docker compose exec -T postgres psql -U vera -d postgres -c "CREATE DATABASE vera_migration_smoke;"
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/vera_migration_smoke" uv run alembic upgrade head
docker compose exec -T postgres psql -U vera -d postgres -c "DROP DATABASE vera_migration_smoke;"
```

Expected: upgrade completes without error (the cleanup revision is a no-op on empty tables).

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/
git commit -m "chore(db): timestamp-gated cleanup of forms pinned to pre-promoted_fields docs"
```

---

### Task 4: Reseed locally and prove the end-to-end promotion path

**Files:** none created — this is runtime verification of Tasks 1-3 against the local stack.

- [ ] **Step 1: Republish schemas**

Run: `just seed-schemas`
Expected output mentions `disease_only ... (published)` with a bumped version (document changed) and `infertility_treatment ... (unchanged)`.

- [ ] **Step 2: Re-seed the demo patient form (exercises `promote_columns` for real)**

Run: `just test_seed_patient_data`
Expected: completes without `InvalidIntakeValue`/validation errors; it builds a form from the freshly published ibv document via the same `promote_columns` path production uses.

- [ ] **Step 3: Boot the API and hit the worklist**

Run: `just api` (needs `LOCAL_KMS_MASTER_KEY` exported), then in another shell fetch the patient-forms list endpoint with a seeded login, or — minimum bar — confirm clean startup and that `GET /healthz` (or the app's health route) responds, then stop it. Any `ValidationError` from stale pinned rows would surface as 500s on the list/detail endpoints; after Task 3's cleanup there are none.

- [ ] **Step 4: Full gate once more**

Run: `just check`
Expected: green.

---

### Task 5: Docs touch-up, simplifier pass, final gate

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/CLAUDE.md` (validator-rules list)

- [ ] **Step 1: Document the new validator rule**

In `forms/CLAUDE.md`, section "Validator rules that bite first", add one bullet after the root-anchored-paths bullet:

```markdown
- `promoted_fields` is REQUIRED and total: a `PromotedFields` block mapping all
  eight patient_form columns; each path must resolve to a leaf AND be a
  `system_fields` target.
```

- [ ] **Step 2: Run the code-simplifier agent (repo-mandated)**

Trigger the `code-simplifier` agent on the changed files (dsl.py, both catalogs, intake.py, patient_forms.py, both test files, the migration). It must not change behavior; review its refinements.

- [ ] **Step 3: Re-run the gate after simplification**

Run: `just check`
Expected: green. Also `git diff data/form_schemas/ibv_form_standard_v2.json` still empty (a simplifier reorder of `PromotedFields` attributes would break byte-identity — revert any such reorder).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(forms): promoted_fields validator rule; simplifier pass"
```

---

## Self-Review Notes

- **Spec coverage:** model+requiredness (Task 1 Steps 1-3), catalog upgrades incl. disease_only sections/task/system_fields (Task 1 Steps 4-5), consumers (Step 6), artifacts byte-identity check (Step 8), parity guard (Task 2), cleanup migration with both gates + FK order + RLS note + no-op downgrade (Task 3), seed/runtime verification (Task 4), docs + mandated simplifier (Task 5). Spec's "no compiler/loader changes" is honored — no `compile_document` edits anywhere.
- **Types:** `items()` defined in Task 1 and consumed with identical signature in Steps 6 and the validator; `PROMOTED_COLUMNS` tuple defined once in the test module and reused by the parity test.
- **Placeholders:** the two `<KEEP GENERATED>` markers in Task 3 are deliberate — alembic mints those ids at Step 1; everything else is concrete.
