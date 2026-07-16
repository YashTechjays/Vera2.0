"""The be79c2989c97 data migration deletes exactly the legacy-shaped
prompt_version rows (composite_json without a top-level "kind") and nothing
else. The test imports LEGACY_DELETE_SQL from the migration module itself, so
it exercises the statement the migration actually runs — the two cannot drift.
Skips without a reachable DB (see conftest)."""

import importlib.util
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import FormSchema, Prompt, PromptVersion, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus

INSURANCE_TYPE = InsuranceType.INFERTILITY_TREATMENT.value

MIGRATION_FILE = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "20260709_1321_be79c2989c97_drop_legacy_pre_promptdocument_prompt_.py"
)

LEGACY_COMPOSITE: dict[str, Any] = {
    "generated_from": "form_schema",
    "dsl_version": "2.1",
    "name": "Legacy Compiled",
    "prompt": "compiled text the application can no longer read",
}

DOCUMENT_COMPOSITE: dict[str, Any] = {
    "kind": "prompt_document",
    "session": {
        "persona": "You are VERA.",
        "goal": "Verify benefits.",
        "base_instructions": "Ask one question at a time.",
    },
    "task_overrides": {},
}

MINIMAL_SCHEMA_JSON: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "Migration Fixture",
    "insurance_type": INSURANCE_TYPE,
    "sections": {
        "basics": {
            "title": "Basics",
            "fields": {
                "plan_type": {
                    "type": "text",
                    "title": "Plan Type",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What type of plan is this?"},
                }
            },
        }
    },
    "tasks": [{"task_key": "main", "title": "Main", "sections": ["basics"]}],
}


def _legacy_delete_sql() -> str:
    spec = importlib.util.spec_from_file_location("migration_be79c2989c97", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sql: str = module.LEGACY_DELETE_SQL
    return sql


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Remove the infertility_treatment prompt + schema families (same order as
    test_seed_prompts._wipe: prompts CASCADE their versions; schema_versions are
    RESTRICT-referenced by prompt_version, so prompts go first)."""
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
            await session.execute(delete(Prompt).where(Prompt.schema_id.in_(schema_ids)))
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids))
            )
            await session.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))


@pytest.fixture
async def clean_family(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(admin_sessionmaker)
    yield
    await _wipe(admin_sessionmaker)


async def test_migration_deletes_only_legacy_rows(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_family: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        schema = FormSchema(insurance_type=INSURANCE_TYPE, name="Migration Fixture")
        session.add(schema)
        await session.flush()
        schema_version = SchemaVersion(
            schema_id=schema.id,
            version=1,
            schema_json=MINIMAL_SCHEMA_JSON,
            status=VersionStatus.PUBLISHED,
        )
        session.add(schema_version)
        await session.flush()
        prompt = Prompt(schema_id=schema.id, name="Migration Fixture Prompt")
        session.add(prompt)
        await session.flush()
        legacy = PromptVersion(
            prompt_id=prompt.id,
            schema_version_id=schema_version.id,
            version=1,
            composite_json=LEGACY_COMPOSITE,
            status=VersionStatus.PUBLISHED,
        )
        document = PromptVersion(
            prompt_id=prompt.id,
            schema_version_id=schema_version.id,
            version=2,
            composite_json=DOCUMENT_COMPOSITE,
            status=VersionStatus.DRAFT,
        )
        session.add_all([legacy, document])
        await session.flush()
        prompt_id = prompt.id
        legacy_id = legacy.id
        document_id = document.id

    sql = _legacy_delete_sql()
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(text(sql))

    async with admin_sessionmaker() as session:
        remaining = (
            (
                await session.execute(
                    select(PromptVersion.id).where(PromptVersion.prompt_id == prompt_id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == [document_id]
        assert legacy_id not in remaining
        # The prompt family itself survives — only version rows are deleted.
        assert (
            await session.execute(select(Prompt.id).where(Prompt.id == prompt_id))
        ).scalar_one() == prompt_id

    # Idempotent: a second run deletes nothing further.
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(text(sql))
    async with admin_sessionmaker() as session:
        remaining = (
            (
                await session.execute(
                    select(PromptVersion.id).where(PromptVersion.prompt_id == prompt_id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == [document_id]
