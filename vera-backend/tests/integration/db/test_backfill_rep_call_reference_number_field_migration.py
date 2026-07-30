"""The rep_call_reference_number_field backfill migration patches every dsl 2.x
schema_version row (both insurance types) that is missing the key, using the
call_reference_number leaf's existing, never-moved location — and leaves alone
(and flags via its guard count) any row whose own sections tree doesn't have
that leaf. Patching must never disturb the order of any EXISTING key at any
nesting level (schema_json is Postgres `json`, not `jsonb`, precisely because
document key order is field order) — this is exercised directly, not just
implied by "validation still passes". The test imports
SELECT_ELIGIBLE_STATEMENTS / UNRESOLVABLE_COUNT_STATEMENTS / patch_document /
abort_if_unresolvable from the migration module itself, so it exercises the
exact logic the migration runs. Skips without a reachable DB (see conftest)."""

import importlib.util
from collections.abc import AsyncGenerator
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import FormSchema, Prompt, SchemaVersion
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
# Deliberately non-alphabetical sibling keys at every level: a jsonb round-trip
# would re-sort these (Postgres jsonb keys sort by length then byte order),
# which is exactly the regression this fixture catches.
ORDER_SENSITIVE_FIELDS: dict[str, Any] = {
    "zzz_last_field": {"type": "text"},
    "call_reference_number": {"type": "text"},
    "aaa_first_field": {"type": "text"},
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
ORDER_SENSITIVE_IBV: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "order-sensitive-ibv",
    "sections": {"insurance_representative": {"fields": ORDER_SENSITIVE_FIELDS}},
}


def _migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_rep_call_ref_backfill", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_backfill(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Mirrors upgrade()'s backfill loop exactly: SELECT the raw json text per
    insurance type, patch it via the migration's own patch_document (order-
    preserving), write it back. Reuses the migration module's own SQL and
    Python logic so the two cannot drift."""
    module = _migration_module()
    async with sessionmaker() as session, session.begin():
        for (section_key, field_key), statement in zip(
            module.PATH_BY_INSURANCE_TYPE.values(),
            module.SELECT_ELIGIBLE_STATEMENTS,
            strict=True,
        ):
            path = f"sections.{section_key}.{field_key}"
            result = await session.execute(text(statement))
            for row_id, schema_json_text in result.all():
                await session.execute(
                    text(module.UPDATE_ROW_SQL),
                    {"doc": module.patch_document(schema_json_text, path), "id": row_id},
                )


async def _guard_counts(sessionmaker: async_sessionmaker[AsyncSession]) -> list[int]:
    module = _migration_module()
    async with sessionmaker() as session:
        counts: list[int] = []
        for statement in module.UNRESOLVABLE_COUNT_STATEMENTS:
            result = await session.execute(text(statement))
            counts.append(result.scalar_one())
        return counts


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """form_schema.insurance_type is globally UNIQUE (one schema family per
    insurance type) — wipe unconditionally by insurance_type, mirroring
    test_promoted_fields_cleanup_migration.py._wipe.

    prompt_version.schema_version_id is RESTRICT (see test_seed_prompts.py._wipe),
    so any stray Prompt/PromptVersion left over from a seed run against this
    insurance_type (e.g. `just seed --prompts`) must be cleared first, or the
    schema_version delete below raises a FK violation."""
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
            await session.execute(delete(Prompt).where(Prompt.schema_id.in_(schema_ids)))
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids))
            )
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
            "order_sensitive": version(ibv_schema, 5, ORDER_SENSITIVE_IBV),
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
        return cast(dict[str, Any], result.scalar_one())


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


async def test_backfill_preserves_existing_key_order(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    backfill_world: dict[str, UUID],
) -> None:
    """The regression this fixture exists to catch: a jsonb round-trip would
    silently re-sort sibling keys (Postgres jsonb orders by length then byte
    value), scrambling the live call's question order and the review UI's
    field order for a form still pinned to this row."""
    await _run_backfill(admin_sessionmaker)
    doc = await _schema_json(admin_sessionmaker, backfill_world["order_sensitive"])

    fields = doc["sections"]["insurance_representative"]["fields"]
    assert list(fields.keys()) == ["zzz_last_field", "call_reference_number", "aaa_first_field"]
    # The new key is simply appended; nothing about the existing document moves.
    assert list(doc.keys())[-1] == "rep_call_reference_number_field"
    assert list(doc.keys())[:-1] == ["dsl_version", "name", "sections"]


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
    # PATH_BY_INSURANCE_TYPE order: infertility_treatment, disease_only.
    assert counts == [1, 0]


class TestAbortIfUnresolvable:
    """upgrade()'s actual abort decision, DB-free: proves the guard's SQL count
    (asserted against a real database above) is correctly wired to a hard
    abort, rather than only asserting the count itself."""

    def test_raises_when_count_is_nonzero(self) -> None:
        module = _migration_module()
        with pytest.raises(RuntimeError, match="infertility_treatment"):
            module.abort_if_unresolvable(1, "infertility_treatment")

    def test_does_not_raise_when_count_is_zero(self) -> None:
        module = _migration_module()
        module.abort_if_unresolvable(0, "disease_only")
