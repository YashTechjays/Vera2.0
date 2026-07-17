"""_seed_form_schemas against a real Postgres: it publishes exactly one version,
is idempotent on re-run, and the partial unique index rejects a second published
version for the same schema. Skips without a reachable DB (see conftest)."""

import json
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas
from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import Leaf, compile_document
from vera_core.models import FormSchema, PatientForm, SchemaVersion, Tenant
from vera_core.models.enums import InsuranceType, VersionStatus

INSURANCE_TYPE = InsuranceType.INFERTILITY_TREATMENT.value
TENANT_SLUG = "seed-form-schemas-versioning-test"


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Remove the infertility_treatment family (global catalog, no tenant scope)."""
    async with sessionmaker() as session, session.begin():
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


@pytest.fixture
async def clean_form_schemas(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(admin_sessionmaker)
    yield
    await _wipe(admin_sessionmaker)


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    """(form_schema, total schema_version, published schema_version) for the family."""
    schemas = (
        await session.execute(
            select(func.count())
            .select_from(FormSchema)
            .where(FormSchema.insurance_type == INSURANCE_TYPE)
        )
    ).scalar_one()
    versions = (
        await session.execute(
            select(
                func.count(), func.count().filter(SchemaVersion.status == VersionStatus.PUBLISHED)
            )
            .select_from(SchemaVersion)
            .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
            .where(FormSchema.insurance_type == INSURANCE_TYPE)
        )
    ).one()
    return schemas, versions[0], versions[1]


async def test_seed_publishes_single_version_and_is_idempotent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_form_schemas: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
    async with admin_sessionmaker() as session:
        assert await _counts(session) == (1, 1, 1)  # one schema, one version, published
        version = (
            await session.execute(
                select(SchemaVersion)
                .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
                .where(FormSchema.insurance_type == INSURANCE_TYPE)
            )
        ).scalar_one()
        assert version.version == 1
        assert version.published_at is not None

    # Re-run with unchanged JSON: no new version (no extra draft), no duplicate schema.
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
    async with admin_sessionmaker() as session:
        assert await _counts(session) == (1, 1, 1)


async def test_second_published_version_rejected(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_form_schemas: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)

    with pytest.raises(IntegrityError):
        async with admin_sessionmaker() as session, session.begin():
            schema = (
                await session.execute(
                    select(FormSchema).where(FormSchema.insurance_type == INSURANCE_TYPE)
                )
            ).scalar_one()
            session.add(
                SchemaVersion(
                    schema_id=schema.id,
                    version=2,
                    schema_json={"name": "dupe"},
                    status=VersionStatus.PUBLISHED,
                )
            )
            await session.flush()


async def _wipe_versioning_tenant(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Drop the pinned patient_form before the tenant. `clean_form_schemas`'s own
    teardown deletes every schema_version for the family (old + new) — a
    patient_form still pinned to the old (RESTRICT FK) one would block that
    delete, so this test's own tenant/patient_form must be gone first."""
    async with sessionmaker() as session, session.begin():
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == TENANT_SLUG))
        ).scalar_one_or_none()
        if tenant_id is not None:
            await session.execute(delete(PatientForm).where(PatientForm.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def test_seed_republishes_new_version_on_content_change_and_keeps_old_version_readable(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_form_schemas: None,
) -> None:
    await _wipe_versioning_tenant(admin_sessionmaker)  # in case a prior run died mid-test
    try:
        # Seed v1, then pin a patient_form to it (simulating an in-flight intake).
        async with admin_sessionmaker() as session, session.begin():
            await _seed_form_schemas(session)
        async with admin_sessionmaker() as session, session.begin():
            v1 = (
                await session.execute(
                    select(SchemaVersion)
                    .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
                    .where(FormSchema.insurance_type == INSURANCE_TYPE)
                )
            ).scalar_one()
            tenant = Tenant(slug=TENANT_SLUG, name="Seed Versioning Test")
            session.add(tenant)
            await session.flush()
            pinned_form = PatientForm(tenant_id=tenant.id, schema_version_id=v1.id)
            session.add(pinned_form)
            await session.flush()
            # Grab everything the later assertions need now — attributes on `v1`
            # expire once this transaction commits at the end of this block.
            v1_id, v1_json = v1.id, v1.schema_json
            v1_schema_id, v1_version = v1.schema_id, v1.version
            pinned_form_id = pinned_form.id

        # Simulate a content change: mutate the built document in-memory (flip one
        # leaf's `required` flag) and reseed through the same code path. Round-trip
        # through compile_document to get the plain dict shape _load_manifest would
        # normally hand back from the compiled JSON artifact (schema_json is stored
        # as a dict, not a FormSchemaDoc).
        _, build_fn = SCHEMAS[INSURANCE_TYPE]
        doc = build_fn()
        patient_name = doc.sections["patient_information"].fields["patient_name"]
        assert isinstance(patient_name, Leaf)
        patient_name.required = False
        mutated_doc = json.loads(compile_document(doc))
        mutated = (INSURANCE_TYPE, mutated_doc["name"], mutated_doc)

        with patch("scripts.seed._load_manifest", return_value=[mutated]):
            async with admin_sessionmaker() as session, session.begin():
                await _seed_form_schemas(session)

        async with admin_sessionmaker() as session:
            # Old version: preserved, demoted, content untouched, still FK-resolvable.
            v1_after = await session.get(SchemaVersion, v1_id)
            assert v1_after is not None
            assert v1_after.status == VersionStatus.DRAFT
            assert v1_after.schema_json == v1_json

            pinned_after = await session.get(PatientForm, pinned_form_id)
            assert pinned_after is not None
            assert pinned_after.schema_version_id == v1_id

            # New version: published, version incremented, content reflects the change.
            v2 = (
                await session.execute(
                    select(SchemaVersion).where(
                        SchemaVersion.schema_id == v1_schema_id,
                        SchemaVersion.status == VersionStatus.PUBLISHED,
                    )
                )
            ).scalar_one()
            assert v2.version == v1_version + 1
            assert v2.id != v1_id
            assert v2.schema_json == mutated_doc
    finally:
        # Must run before clean_form_schemas' own teardown deletes the
        # schema_version rows, or the still-pinned patient_form trips the
        # RESTRICT FK and leaves this test's rows orphaned for the next test.
        await _wipe_versioning_tenant(admin_sessionmaker)
