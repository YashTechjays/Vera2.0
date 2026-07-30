# Rep Call Reference Number Field Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `rep_call_reference_number_field` as a required, generalized DSL concept marking where a form schema's representative call-reference-number leaf lives, and keep every already-persisted schema document parseable under the new requirement.

**Architecture:** A new required `str` field on the `FormSchemaDoc` pydantic model (`vera_core/forms/dsl.py`), validated the same way `system_fields` paths are (must resolve to a leaf); both catalog modules (`ibv_standard.py`, `disease_only.py`) point it at their existing `call_reference_number` leaf; compiled artifacts are regenerated. A companion Alembic data migration backfills the same value into every already-persisted `schema_version` row so existing/in-flight forms don't break when re-parsed.

**Tech Stack:** Python 3.12, pydantic v2, SQLAlchemy 2.x (async ORM + sync Alembic migrations), pytest/pytest-asyncio, Postgres (`json`/`jsonb`).

## Global Constraints

- Full local gate is `just check` (ruff format --check + ruff check + mypy --strict + pytest) — run it verbatim before calling anything done, never a hand-picked subset.
- Root-anchored path convention: every schema path is `sections.<key>(.<key>)+`, byte-identical to `field_answer.field_path`.
- Alembic revision IDs are random hex minted by `just makemigration` — never hand-numbered.
- Migrations must be idempotent-safe to re-run.
- Repo-wide mandatory rule: after implementation, run the code-simplifier (`code-simplifier@claude-plugins-official`, triggered via "simplify code") on the changed code in this same session, then re-run `just check`, before declaring the work done.
- `docs/superpowers/specs/2026-07-21-rep-call-reference-number-field-design.md` is the approved design this plan implements — consult it for the full rationale behind each decision below.

---

### Task 1: `rep_call_reference_number_field` — DSL model, validator, both catalogs, compiled artifacts

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py:467-488` (`FormSchemaDoc` fields), `:717-736` (`_validate_document`)
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py:1025-1041`
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/disease_only.py:409-422`
- Modify (generated, do not hand-edit content — just regenerate): `vera-backend/data/form_schemas/ibv_form_standard_v2.json`, `vera-backend/data/form_schemas/disease_only_verification.json`
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py:36-73` (`minimal_doc` fixture), `:150` (`TestCompiledArtifacts`), `:374` area (`TestDocumentValidation`)
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/CLAUDE.md`

**Interfaces:**
- Produces: `FormSchemaDoc.rep_call_reference_number_field: str` — a required root-anchored leaf path, present on every parsed/compiled dsl 2.1 document going forward. `catalog.SCHEMAS["infertility_treatment"][1]().rep_call_reference_number_field == "sections.insurance_representative.call_reference_number"`; `catalog.SCHEMAS["disease_only"][1]().rep_call_reference_number_field == "sections.representative_details.call_reference_number"`.

- [ ] **Step 1: Write the failing tests**

In `vera-backend/tests/unit/forms/test_schema_dsl.py`, update the shared fixture so every test building a document includes the new required key — find:

```python
        "system_fields": {"plan_type": "sections.basics.plan_type"},
        "promoted_fields": dict.fromkeys(PROMOTED_COLUMNS, "sections.basics.plan_type"),
        "sections": {
```

and change it to:

```python
        "system_fields": {"plan_type": "sections.basics.plan_type"},
        "promoted_fields": dict.fromkeys(PROMOTED_COLUMNS, "sections.basics.plan_type"),
        "rep_call_reference_number_field": "sections.basics.plan_type",
        "sections": {
```

Add two new tests inside `class TestDocumentValidation`, right after `test_promoted_fields_rejects_path_not_a_leaf` (the block ending around line 402-403, just before `test_promoted_fields_rejects_path_not_backed_by_system_fields`):

```python
    def test_rep_call_reference_number_field_is_required(self) -> None:
        doc = minimal_doc()
        del doc["rep_call_reference_number_field"]
        with pytest.raises(ValidationError):
            FormSchemaDoc.model_validate(doc)

    def test_rep_call_reference_number_field_rejects_path_not_a_leaf(self) -> None:
        doc = minimal_doc()
        doc["rep_call_reference_number_field"] = "sections.basics.missing"
        with pytest.raises(ValidationError, match="does not resolve to a leaf"):
            FormSchemaDoc.model_validate(doc)
```

Add two new tests inside `class TestCompiledArtifacts`, right after `test_disease_only_promotes_the_full_column_set` (ends around line 150):

```python
    def test_ibv_rep_call_reference_number_field(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        assert (
            doc.rep_call_reference_number_field
            == "sections.insurance_representative.call_reference_number"
        )

    def test_disease_only_rep_call_reference_number_field(self) -> None:
        doc = SCHEMAS["disease_only"][1]()
        assert (
            doc.rep_call_reference_number_field
            == "sections.representative_details.call_reference_number"
        )
```

- [ ] **Step 2: Run the tests and confirm they fail for the expected reason**

Run: `cd vera-backend && uv run pytest tests/unit/forms/test_schema_dsl.py -v`

Expected: many failures (not just the four new tests) — every test that constructs a document via `minimal_doc()` now fails with a pydantic `ValidationError: Extra inputs are not permitted` for `rep_call_reference_number_field` (the model doesn't know this key yet), and `test_ibv_rep_call_reference_number_field` / `test_disease_only_rep_call_reference_number_field` fail with `AttributeError: 'FormSchemaDoc' object has no attribute 'rep_call_reference_number_field'`. This wide failure is expected at this point — the whole file only goes green again after Step 3.

- [ ] **Step 3: Implement the model + validator change**

In `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py`, find:

```python
    promoted_fields: PromotedFields
    # Session-wide STT vocabulary, fed verbatim to deepgram.STTv2(keyterms=...)
    # at voice-session build; applies to every task. Static domain terms only.
    stt_key_terms: list[str] | None = None
```

and change it to:

```python
    promoted_fields: PromotedFields
    # Single root-anchored leaf path: where this insurance type's rep call reference
    # number lives. The one generalized place retry/resume logic reads regardless of
    # insurance type — empty/never-collected on a prior attempt means no valid retry
    # state (treat as a fresh call).
    rep_call_reference_number_field: str
    # Session-wide STT vocabulary, fed verbatim to deepgram.STTv2(keyterms=...)
    # at voice-session build; applies to every task. Static domain terms only.
    stt_key_terms: list[str] | None = None
```

In the same file, find the `_validate_document` method's promoted-fields block:

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

and add, immediately after it:

```python

        # rep call reference number field — single root-anchored path naming which
        # leaf holds the representative's call reference number. Only checked for
        # leaf existence: unlike system_fields/promoted_fields this value is
        # collected DURING the call, not known beforehand, so it deliberately does
        # NOT need to be a system_fields target.
        if self.rep_call_reference_number_field not in leaves:
            errors.append(
                "rep_call_reference_number_field: "
                f"{self.rep_call_reference_number_field!r} does not resolve to a leaf"
            )
```

- [ ] **Step 4: Wire both catalog modules**

In `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`, find:

```python
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.insurance_information.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
        stt_key_terms=[
```

and change it to:

```python
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.insurance_information.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
        rep_call_reference_number_field="sections.insurance_representative.call_reference_number",
        stt_key_terms=[
```

In `vera-backend/packages/vera_core/src/vera_core/forms/catalog/disease_only.py`, find:

```python
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.policy_details.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
        shared_conditions={
```

and change it to:

```python
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.policy_details.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
        rep_call_reference_number_field="sections.representative_details.call_reference_number",
        shared_conditions={
```

- [ ] **Step 5: Recompile the schema artifacts**

Run: `cd vera-backend && just compile-schemas`

Expected: no output on success; `git diff --stat data/form_schemas/` shows both `ibv_form_standard_v2.json` and `disease_only_verification.json` changed (one new line each, `"rep_call_reference_number_field": "..."`, positioned between `promoted_fields` and `stt_key_terms`/`shared_conditions`).

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `cd vera-backend && uv run pytest tests/unit/forms/test_schema_dsl.py -v`

Expected: PASS, full file green (includes `test_committed_artifact_is_fresh` and `test_round_trip`, which now compare against the artifacts regenerated in Step 5).

- [ ] **Step 7: Update the DSL docs**

In `vera-backend/packages/vera_core/src/vera_core/forms/CLAUDE.md`, find:

```markdown
- `promoted_fields` is REQUIRED and total: a `PromotedFields` block mapping all
  eight patient_form columns; each path must resolve to a leaf AND be a
  `system_fields` target.
```

and change it to:

```markdown
- `promoted_fields` is REQUIRED and total: a `PromotedFields` block mapping all
  eight patient_form columns; each path must resolve to a leaf AND be a
  `system_fields` target.
- `rep_call_reference_number_field` is REQUIRED: a single root-anchored path
  naming which leaf holds the representative's call reference number. Only
  checked for leaf existence — unlike `promoted_fields` it does NOT need to be
  a `system_fields` target (this value is collected during the call, not known
  beforehand).
```

Find:

```markdown
- **Document key order IS field/section order** (spec §4.1). That's why
  `schema_version.schema_json` / `prompt_version.composite_json` are Postgres
  `JSON`, not `JSONB` (JSONB re-sorts keys) — don't "normalize" them back.
```

and add, right after the "Semantics worth remembering" bullet list's last item (after the `system_fields` bullet that ends "wins over role in the UI color coding."):

```markdown
- `rep_call_reference_number_field` is the one generalized place to look for a
  schema's rep call reference number, regardless of insurance type — a retry
  mechanism reads it to decide whether a previous attempt already captured a
  valid reference number (empty/never-collected means treat the retry as a
  fresh call). See
  `docs/superpowers/specs/2026-07-21-rep-call-reference-number-field-design.md`.
```

- [ ] **Step 8: Commit**

```bash
cd vera-backend && git add \
  packages/vera_core/src/vera_core/forms/dsl.py \
  packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py \
  packages/vera_core/src/vera_core/forms/catalog/disease_only.py \
  packages/vera_core/src/vera_core/forms/CLAUDE.md \
  data/form_schemas/ibv_form_standard_v2.json \
  data/form_schemas/disease_only_verification.json \
  tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): add rep_call_reference_number_field DSL concept"
```

---

### Task 2: Backfill migration for already-persisted `schema_version` rows

**Files:**
- Create: `vera-backend/migrations/versions/<generated>_backfill_rep_call_reference_number_field.py`
- Test: `vera-backend/tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py`

**Interfaces:**
- Consumes: nothing from Task 1 at the code level (pure SQL migration, no `dsl.py` import) — but implements the exact key name (`rep_call_reference_number_field`) and paths Task 1 established.
- Produces: `UPDATE_STATEMENTS: tuple[str, ...]` and `UNRESOLVABLE_COUNT_STATEMENTS: tuple[str, ...]` module-level constants in the migration file, imported by the integration test.

- [ ] **Step 1: Generate the migration file**

Run: `cd vera-backend && just makemigration "backfill rep call reference number field"`

Expected: a new file appears under `migrations/versions/`, named like `<timestamp>_<hex>_backfill_rep_call_reference_number_field.py`, with `down_revision` set to whatever the current head is (`c8921c9301da` as of this plan's authoring — if other migrations have landed since, the tool will chain off the new head instead; leave it as generated). The autogenerated `upgrade()`/`downgrade()` bodies will be empty (`pass`) since there is no ORM model diff — that's expected; Step 2 replaces them.

- [ ] **Step 2: Replace the generated file's content**

Keep the file's generated `revision`, `down_revision`, `branch_labels`, `depends_on` lines exactly as generated. Replace everything else (docstring + imports + body) with:

```python
"""backfill rep_call_reference_number_field into existing schema_version rows

`rep_call_reference_number_field` became a REQUIRED top-level key on every dsl
2.x FormSchemaDoc (vera_core/forms/dsl.py). schema_version rows are immutable
and patient_form.schema_version_id RESTRICTs forever (see GitHub issue #114 for
the underlying gap: FormSchemaDoc validation isn't scoped per dsl_version), so
any already-persisted schema_version document missing this key fails
FormSchemaDoc validation the next time it's re-parsed — dispute-resolve
(patient_forms.py), retry dispatch (queue_dispatcher.py), and mid-call answer
recompute (field_answers.py / worker_events.py) all re-validate a form's own
pinned document.

Unlike the promoted_fields precedent (9d09f73f7357, a destructive delete), the
value here is safely backfillable: `call_reference_number` has lived at the
exact same leaf path since each schema's very first commit
(infertility_treatment: 3675c19d, disease_only: eaf1484e) and has never moved,
so every historical row can be patched with the correct value in place instead
of losing data.

Two independent guards:
- predicate: only rows whose OWN sections tree still has a call_reference_number
  leaf at the expected path for that insurance_type are eligible;
- idempotency: only rows that don't already carry the key are touched, so
  re-running this migration is a no-op.

Before mutating anything, upgrade() first counts rows that are dsl 2.x, missing
the key, AND do NOT resolve the expected leaf — if any exist, it aborts loudly
rather than silently leaving (or mismatching) a row; this should be impossible
given the path history above, but the check costs nothing and this table holds
compiled prompts for a HIPAA-regulated voice pipeline.

Only the two insurance types known at authoring time are covered
(infertility_treatment, disease_only) — any insurance type added after this
point is authored with the field already required (dsl.py enforces it at
compile time), so it never needs backfilling.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "REPLACE_WITH_GENERATED_REVISION"
down_revision: str | None = "REPLACE_WITH_GENERATED_DOWN_REVISION"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# insurance_type -> (section_key, field_key) for the pre-existing
# call_reference_number leaf.
_PATH_BY_INSURANCE_TYPE: dict[str, tuple[str, str]] = {
    "infertility_treatment": ("insurance_representative", "call_reference_number"),
    "disease_only": ("representative_details", "call_reference_number"),
}

# Rows eligible for backfill (dsl 2.x, key missing) whose sections tree does NOT
# have the expected leaf — should always count 0; a non-zero count aborts
# upgrade() before anything is mutated.
UNRESOLVABLE_COUNT_STATEMENTS: tuple[str, ...] = tuple(
    f"""
    SELECT count(*)
    FROM schema_version sv
    JOIN form_schema fs ON fs.id = sv.schema_id
    WHERE fs.insurance_type = '{insurance_type}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
      AND NOT COALESCE(
          ((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}',
          FALSE
      )
    """
    for insurance_type, (section_key, field_key) in _PATH_BY_INSURANCE_TYPE.items()
)

# Exposed as a module constant so the integration test
# (tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py)
# executes the EXACT statements the migration runs — the two cannot drift.
UPDATE_STATEMENTS: tuple[str, ...] = tuple(
    f"""
    UPDATE schema_version sv
    SET schema_json = (
        (sv.schema_json::jsonb) || jsonb_build_object(
            'rep_call_reference_number_field', 'sections.{section_key}.{field_key}'
        )
    )::json
    FROM form_schema fs
    WHERE fs.id = sv.schema_id
      AND fs.insurance_type = '{insurance_type}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
      AND COALESCE(
          ((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}',
          FALSE
      )
    """
    for insurance_type, (section_key, field_key) in _PATH_BY_INSURANCE_TYPE.items()
)


def upgrade() -> None:
    conn = op.get_bind()
    for statement in UNRESOLVABLE_COUNT_STATEMENTS:
        remaining = conn.exec_driver_sql(statement).scalar_one()
        if remaining:
            raise RuntimeError(
                "backfill_rep_call_reference_number_field: "
                f"{remaining} schema_version row(s) are dsl 2.x, missing "
                "rep_call_reference_number_field, and do NOT resolve the "
                "expected call_reference_number leaf — investigate before "
                "re-running (see this migration's docstring)."
            )
    for statement in UPDATE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Backfilling only ever adds a key every dsl 2.x document is required to
    # carry anyway (dsl.py); nothing depends on removing it again.
    pass
```

Replace `REPLACE_WITH_GENERATED_REVISION` / `REPLACE_WITH_GENERATED_DOWN_REVISION` by leaving the file's own generated `revision`/`down_revision` lines untouched — i.e. only overwrite the docstring/imports/body shown above, don't actually paste the literal placeholder strings.

- [ ] **Step 3: Write the integration test**

Create `vera-backend/tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py`:

```python
"""The rep_call_reference_number_field backfill migration patches every dsl 2.x
schema_version row (both insurance types) that is missing the key, using the
call_reference_number leaf's existing, never-moved location — and leaves alone
(and flags via its guard count) any row whose own sections tree doesn't have
that leaf. The test imports UPDATE_STATEMENTS / UNRESOLVABLE_COUNT_STATEMENTS
from the migration module itself, so it exercises the exact statements the
migration runs. Skips without a reachable DB (see conftest)."""

import importlib.util
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus

IBV = InsuranceType.INFERTILITY_TREATMENT.value
DISEASE_ONLY = InsuranceType.DISEASE_ONLY.value

# Random-hex prefix is minted at `just makemigration` time — glob, don't hardcode.
MIGRATION_FILE = next(
    (Path(__file__).resolve().parents[3] / "migrations" / "versions").glob(
        "*_backfill_rep_call_reference_number_field*.py"
    )
)

IBV_SECTIONS_WITH_LEAF: dict[str, Any] = {
    "insurance_representative": {"fields": {"call_reference_number": {"type": "text"}}}
}
DISEASE_SECTIONS_WITH_LEAF: dict[str, Any] = {
    "representative_details": {"fields": {"call_reference_number": {"type": "text"}}}
}
SECTIONS_WITHOUT_LEAF: dict[str, Any] = {
    "insurance_representative": {"fields": {"rep_name": {"type": "text"}}}
}

MISSING_KEY_IBV: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "missing-key-ibv",
    "sections": IBV_SECTIONS_WITH_LEAF,
}
MISSING_KEY_DISEASE: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "missing-key-disease",
    "sections": DISEASE_SECTIONS_WITH_LEAF,
}
ALREADY_HAS_KEY: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "already-has-key",
    "rep_call_reference_number_field": "sections.insurance_representative.call_reference_number",
    "sections": IBV_SECTIONS_WITH_LEAF,
}
V1_DOC: dict[str, Any] = {"name": "legacy v1", "sections": []}  # no dsl_version
UNRESOLVABLE_IBV: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "unresolvable-ibv",
    "sections": SECTIONS_WITHOUT_LEAF,
}


def _update_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("migration_rep_call_ref_backfill", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: tuple[str, ...] = module.UPDATE_STATEMENTS
    return statements


def _unresolvable_count_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("migration_rep_call_ref_backfill", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: tuple[str, ...] = module.UNRESOLVABLE_COUNT_STATEMENTS
    return statements


async def _run_backfill(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session, session.begin():
        for statement in _update_statements():
            await session.execute(text(statement))


async def _guard_counts(sessionmaker: async_sessionmaker[AsyncSession]) -> list[int]:
    async with sessionmaker() as session:
        counts: list[int] = []
        for statement in _unresolvable_count_statements():
            result = await session.execute(text(statement))
            counts.append(result.scalar_one())
        return counts


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """form_schema.insurance_type is globally UNIQUE (one schema family per
    insurance type) — wipe unconditionally by insurance_type, mirroring
    test_promoted_fields_cleanup_migration.py._wipe."""
    async with sessionmaker() as session, session.begin():
        schema_ids = (
            (
                await session.execute(
                    select(FormSchema.id).where(FormSchema.insurance_type.in_([IBV, DISEASE_ONLY]))
                )
            )
            .scalars()
            .all()
        )
        if schema_ids:
            await session.execute(delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids)))
            await session.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))


@pytest.fixture
async def backfill_world(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, UUID]]:
    await _wipe(admin_sessionmaker)
    async with admin_sessionmaker() as session, session.begin():
        ibv_schema = FormSchema(insurance_type=IBV, name="Rep Call Ref Backfill Fixture (IBV)")
        disease_schema = FormSchema(
            insurance_type=DISEASE_ONLY, name="Rep Call Ref Backfill Fixture (Disease)"
        )
        session.add_all([ibv_schema, disease_schema])
        await session.flush()

        def version(schema: FormSchema, number: int, doc: dict[str, Any]) -> SchemaVersion:
            row = SchemaVersion(
                schema_id=schema.id, version=number, schema_json=doc, status=VersionStatus.DRAFT
            )
            session.add(row)
            return row

        rows = {
            "missing_key": version(ibv_schema, 1, MISSING_KEY_IBV),
            "already_has_key": version(ibv_schema, 2, ALREADY_HAS_KEY),
            "v1": version(ibv_schema, 3, V1_DOC),
            "unresolvable": version(ibv_schema, 4, UNRESOLVABLE_IBV),
            "missing_key_disease": version(disease_schema, 1, MISSING_KEY_DISEASE),
        }
        await session.flush()
        ids = {key: row.id for key, row in rows.items()}
    yield ids
    await _wipe(admin_sessionmaker)


async def _schema_json(
    sessionmaker: async_sessionmaker[AsyncSession], version_id: UUID
) -> dict[str, Any]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == version_id)
        )
        return result.scalar_one()


async def test_backfills_rows_missing_the_key_when_the_leaf_resolves(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    backfill_world: dict[str, UUID],
) -> None:
    await _run_backfill(admin_sessionmaker)

    ibv_doc = await _schema_json(admin_sessionmaker, backfill_world["missing_key"])
    assert (
        ibv_doc["rep_call_reference_number_field"]
        == "sections.insurance_representative.call_reference_number"
    )

    disease_doc = await _schema_json(admin_sessionmaker, backfill_world["missing_key_disease"])
    assert (
        disease_doc["rep_call_reference_number_field"]
        == "sections.representative_details.call_reference_number"
    )

    # The predicate excludes rows whose sections tree doesn't have the leaf.
    unresolvable_doc = await _schema_json(admin_sessionmaker, backfill_world["unresolvable"])
    assert "rep_call_reference_number_field" not in unresolvable_doc


async def test_row_already_carrying_the_key_is_untouched(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    backfill_world: dict[str, UUID],
) -> None:
    before = await _schema_json(admin_sessionmaker, backfill_world["already_has_key"])
    await _run_backfill(admin_sessionmaker)
    after = await _schema_json(admin_sessionmaker, backfill_world["already_has_key"])
    assert after == before


async def test_v1_document_is_ignored(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    backfill_world: dict[str, UUID],
) -> None:
    before = await _schema_json(admin_sessionmaker, backfill_world["v1"])
    await _run_backfill(admin_sessionmaker)
    after = await _schema_json(admin_sessionmaker, backfill_world["v1"])
    assert after == before


async def test_second_run_is_a_no_op(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    backfill_world: dict[str, UUID],
) -> None:
    await _run_backfill(admin_sessionmaker)
    first = await _schema_json(admin_sessionmaker, backfill_world["missing_key"])
    await _run_backfill(admin_sessionmaker)
    second = await _schema_json(admin_sessionmaker, backfill_world["missing_key"])
    assert second == first


async def test_guard_counts_the_unresolvable_row_per_insurance_type(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    backfill_world: dict[str, UUID],
) -> None:
    counts = await _guard_counts(admin_sessionmaker)
    # _PATH_BY_INSURANCE_TYPE order: infertility_treatment, disease_only.
    assert counts == [1, 0]
```

- [ ] **Step 4: Confirm no DB is reachable path still collects correctly (skip check)**

Run: `cd vera-backend && uv run pytest tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py -v`

Expected: either all 5 tests PASS (if `just up` has been run and Postgres is reachable), or all 5 SKIP with a message like "postgres not reachable — run `just up`" (per `tests/integration/conftest.py`). Both are acceptable at this step; if Postgres is not running, run `cd vera-backend && just up` first, then re-run this command and confirm PASS.

- [ ] **Step 5: Commit**

```bash
cd vera-backend && git add \
  migrations/versions/*_backfill_rep_call_reference_number_field.py \
  tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py
git commit -m "feat(db): backfill rep_call_reference_number_field into existing schema_version rows"
```

---

### Task 3: Full verification pass

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything produced by Tasks 1-2.

- [ ] **Step 1: Run the mandatory code-simplifier pass**

Per the repo-root `CLAUDE.md`, trigger the code-simplifier on the changes made in Tasks 1-2 (say "simplify code" — this launches the `code-simplifier@claude-plugins-official` agent). It targets recently modified code by default; point it at the files touched in Tasks 1-2 if it doesn't pick them up automatically:
`packages/vera_core/src/vera_core/forms/dsl.py`, `catalog/ibv_standard.py`, `catalog/disease_only.py`, `forms/CLAUDE.md`, `data/form_schemas/*.json`, `tests/unit/forms/test_schema_dsl.py`, the new migration file, `tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py`.

Confirm it reports no behavior changes — only clarity/consistency edits (if any).

- [ ] **Step 2: Re-run the full gate**

Run: `cd vera-backend && just check`

Expected: PASS (ruff format --check, ruff check, mypy --strict, pytest all green). If the code-simplifier changed anything, this step catches regressions before they're committed.

If `just up` was not already running for Task 2's integration test, also run `cd vera-backend && just up` and re-run `uv run pytest tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py -v` to confirm the integration test passes for real (not just skipped) at least once.

- [ ] **Step 3: Verify the schema republishes cleanly against a real local DB**

With `just up` running and the DB migrated (`cd vera-backend && just migrate`), run: `cd vera-backend && just seed-schemas`

Expected: output confirms both `infertility_treatment` and `disease_only` published a new `schema_version` (their compiled document text changed in Task 1, so `_same_document`'s order-sensitive comparison in `scripts/seed.py` correctly detects a diff and republishes rather than reporting "unchanged"). This is the real-world analogue of Task 1's `test_committed_artifact_is_fresh`/`test_round_trip` tests, confirmed against an actual database instead of just the artifact files.

- [ ] **Step 4: Commit any simplification changes (only if the simplifier changed anything)**

```bash
cd vera-backend && git add -A
git status --short   # review — only the files listed in Step 1 should appear
git commit -m "refactor: simplify rep_call_reference_number_field changes"
```

If the code-simplifier made no changes, skip this step — there is nothing to commit.
