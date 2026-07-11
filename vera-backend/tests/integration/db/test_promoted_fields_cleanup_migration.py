"""The promoted_fields cleanup migration deletes exactly the patient_form rows
pinned to a dsl 2.x schema_version whose promoted_fields block is incomplete
AND that were created before the 2026-07-31 cutoff — and nothing else. The test
imports DELETE_STATEMENTS from the migration module itself, so it exercises the
statements the migration actually runs — the two cannot drift. Skips without a
reachable DB (see conftest)."""

import importlib.util
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import (
    Call,
    ExportArtifact,
    FieldAnswer,
    FormSchema,
    PatientForm,
    SchemaVersion,
    Tenant,
)
from vera_core.models.enums import (
    AnswerSource,
    CallStatus,
    ExportFormat,
    InsuranceType,
    VersionStatus,
)

INSURANCE_TYPE = InsuranceType.INFERTILITY_TREATMENT.value
TENANT_SLUG = "promoted-cleanup-mig-test"

# Random-hex prefix is minted at `just makemigration` time — glob, don't hardcode.
MIGRATION_FILE = next(
    (Path(__file__).resolve().parents[3] / "migrations" / "versions").glob(
        "*_delete_forms_pinned_to_pre_promoted_*.py"
    )
)

_ALL_PROMOTED = dict.fromkeys(
    (
        "patient_name",
        "patient_dob",
        "chart_number",
        "appointment_date",
        "appointment_type",
        "member_id",
        "insurance_provider",
        "insurance_provider_phone_number",
    ),
    "sections.x.y",
)
# The migration inspects the RAW pinned JSON — these fixtures only need the
# shape the predicate reads (dsl_version + promoted_fields keys), not a fully
# valid FormSchemaDoc.
BLOCKLESS_V2: dict[str, Any] = {"dsl_version": "2.1", "name": "blockless", "sections": {}}
INCOMPLETE_V2: dict[str, Any] = {
    **BLOCKLESS_V2,
    "name": "incomplete",
    "promoted_fields": {"patient_name": "sections.x.y"},  # 1 of 8 keys
}
COMPLETE_V2: dict[str, Any] = {**BLOCKLESS_V2, "name": "complete", "promoted_fields": _ALL_PROMOTED}
V1_DOC: dict[str, Any] = {"name": "legacy v1", "sections": []}  # no dsl_version


def _delete_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("migration_promoted_cleanup", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: tuple[str, ...] = module.DELETE_STATEMENTS
    return statements


async def _run_cleanup(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session, session.begin():
        for statement in _delete_statements():
            await session.execute(text(statement))


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Delete the fixture tenant's world in FK order (calls/exports → forms →
    versions → schema → tenant).

    `form_schema.insurance_type` is globally UNIQUE (one schema family per
    insurance type, ADR §7) — a fresh `FormSchema(insurance_type=INSURANCE_TYPE)`
    collides with whatever row a sibling test or `scripts/seed.py` left behind,
    not just a same-named row. So the schema+version family is matched by
    `insurance_type` and wiped unconditionally (mirrors
    test_prompt_version_data_migration.py._wipe), independent of whether this
    fixture's own tenant happens to exist yet."""
    async with sessionmaker() as session, session.begin():
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == TENANT_SLUG))
        ).scalar_one_or_none()
        if tenant_id is not None:
            await session.execute(delete(Call).where(Call.tenant_id == tenant_id))
            await session.execute(
                delete(ExportArtifact).where(ExportArtifact.tenant_id == tenant_id)
            )
            await session.execute(delete(PatientForm).where(PatientForm.tenant_id == tenant_id))
        schema_ids = (
            (
                await session.execute(
                    select(FormSchema.id).where(FormSchema.insurance_type == INSURANCE_TYPE)
                )
            )
            .scalars()
            .all()
        )
        if schema_ids:
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids))
            )
            await session.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))
        if tenant_id is not None:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.fixture
async def cleanup_world(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, UUID]]:
    """Seed one tenant + four pinned forms covering every predicate branch, plus
    call/export/answer children on the doomed form. Yields the ids the test
    asserts against."""
    await _wipe(admin_sessionmaker)
    async with admin_sessionmaker() as session, session.begin():
        tenant = Tenant(slug=TENANT_SLUG, name="Promoted Cleanup Test", status="active")
        session.add(tenant)
        await session.flush()
        schema = FormSchema(insurance_type=INSURANCE_TYPE, name="Promoted Cleanup Fixture")
        session.add(schema)
        await session.flush()
        versions = {
            key: SchemaVersion(
                schema_id=schema.id, version=i + 1, schema_json=doc, status=VersionStatus.DRAFT
            )
            for i, (key, doc) in enumerate(
                [
                    ("blockless", BLOCKLESS_V2),
                    ("incomplete", INCOMPLETE_V2),
                    ("complete", COMPLETE_V2),
                    ("v1", V1_DOC),
                ]
            )
        }
        session.add_all(versions.values())
        await session.flush()

        def form(version_key: str, **kwargs: Any) -> PatientForm:
            row = PatientForm(
                tenant_id=tenant.id, schema_version_id=versions[version_key].id, **kwargs
            )
            session.add(row)
            return row

        stale = form("blockless")  # created now (< cutoff) → DELETED
        stale_incomplete = form("incomplete")  # partial block → DELETED
        survivor_complete = form("complete")  # full block → survives
        survivor_v1 = form("v1")  # not dsl 2.x → survives
        # Matches the predicate but post-dates the 2026-07-31 cutoff → survives.
        survivor_late = form("blockless", created_at=datetime(2026, 8, 15, tzinfo=UTC))
        await session.flush()

        # Children on the doomed form: RESTRICT FKs (call, export_artifact) the
        # migration deletes explicitly, CASCADE (field_answer) it relies on.
        session.add(
            Call(
                tenant_id=tenant.id,
                form_id=stale.id,
                current_status=CallStatus.COMPLETED,
            )
        )
        session.add(
            ExportArtifact(
                tenant_id=tenant.id,
                form_id=stale.id,
                format=ExportFormat.PDF,
                gcs_uri="gs://test/promoted-cleanup",
            )
        )
        session.add(
            FieldAnswer(
                tenant_id=tenant.id,
                form_id=stale.id,
                field_path="sections.x.y",
                value={"value": "fixture"},
                source=AnswerSource.INTAKE,
            )
        )
        await session.flush()
        ids = {
            "tenant": tenant.id,
            "stale": stale.id,
            "stale_incomplete": stale_incomplete.id,
            "survivor_complete": survivor_complete.id,
            "survivor_v1": survivor_v1.id,
            "survivor_late": survivor_late.id,
        }
    yield ids
    await _wipe(admin_sessionmaker)


async def _form_ids(sessionmaker: async_sessionmaker[AsyncSession], tenant_id: UUID) -> set[UUID]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(select(PatientForm.id).where(PatientForm.tenant_id == tenant_id))
        ).scalars()
        return set(rows)


async def test_deletes_only_pre_cutoff_forms_pinned_to_incomplete_blocks(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    cleanup_world: dict[str, UUID],
) -> None:
    await _run_cleanup(admin_sessionmaker)

    remaining = await _form_ids(admin_sessionmaker, cleanup_world["tenant"])
    assert remaining == {
        cleanup_world["survivor_complete"],
        cleanup_world["survivor_v1"],
        cleanup_world["survivor_late"],
    }

    async with admin_sessionmaker() as session:
        # RESTRICT children were deleted first, CASCADE children followed the form.
        for model in (Call, ExportArtifact, FieldAnswer):
            rows = (
                await session.execute(
                    select(model.id).where(model.tenant_id == cleanup_world["tenant"])
                )
            ).all()
            assert rows == [], f"{model.__tablename__} rows survived"
        # schema_version rows are never deleted — only the forms pinned to them.
        versions = (
            await session.execute(
                select(SchemaVersion.id)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(FormSchema.name == "Promoted Cleanup Fixture")
            )
        ).all()
        assert len(versions) == 4


async def test_second_run_is_a_no_op(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    cleanup_world: dict[str, UUID],
) -> None:
    await _run_cleanup(admin_sessionmaker)
    before = await _form_ids(admin_sessionmaker, cleanup_world["tenant"])
    await _run_cleanup(admin_sessionmaker)
    assert await _form_ids(admin_sessionmaker, cleanup_world["tenant"]) == before
