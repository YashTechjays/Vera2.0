"""The percent-answer backfill migration rewrites every stored `percent`-leaf answer to
its canonical "<n>%" form — across ALL rows, not just `is_current`, because a superseded
intake/human row is still the dispute baseline (`field_answers.baseline_value` filters on
`source` and NOT on `is_current`), so leaving history alone would manufacture a false
dispute on every historical form.

Exercises the migration module's OWN helpers and SQL (imported by glob, since the
revision's hex prefix is minted at `just makemigration` time) so the test and the
migration cannot drift, plus:
  - a mixed-shape round trip on a real database, including a non-current history row;
  - re-running is a clean no-op (idempotency);
  - a v1-pinned form is untouched;
  - the BYPASSRLS privilege guard, both as a pure decision and against the real
    non-superuser role, because without it FORCE RLS on `field_answer` makes the whole
    migration update ZERO rows while exiting green.

Skips without a reachable DB (see conftest)."""

import importlib.util
import json
from collections.abc import AsyncGenerator
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import FormSchema, Prompt, SchemaVersion
from vera_core.models.enums import AnswerSource, InsuranceType, VersionStatus
from vera_core.models.field_answer import FieldAnswer
from vera_core.models.patient_form import PatientForm
from vera_core.models.tenant import Tenant

IBV = InsuranceType.INFERTILITY_TREATMENT.value

# Random-hex prefix is minted at `just makemigration` time — glob, don't hardcode.
MIGRATION_FILE = next(
    (Path(__file__).resolve().parents[3] / "migrations" / "versions").glob(
        "*_backfill_percent_field_answers*.py"
    )
)

# A percent leaf at section level and another nested two groups deep, so the frozen path
# walk is proven to skip the `fields` CONTAINER key at every level. Plus a currency
# sibling, which must be left alone (the walk is keyed on `type`).
V2_DOC: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "percent-backfill-fixture",
    "sections": {
        "coverage": {
            "fields": {
                "plan_coinsurance": {"type": "percent"},
                "deductible": {"type": "currency"},
                "svc": {
                    "type": "group",
                    "fields": {
                        "cpt_1": {
                            "type": "group",
                            "fields": {
                                "coinsurance": {"type": "percent"},
                                "copay": {"type": "currency"},
                            },
                        }
                    },
                },
            }
        }
    },
}
V1_DOC: dict[str, Any] = {"name": "legacy v1", "sections": []}  # no dsl_version

_TENANT_SLUG = "percent-backfill-fixture"
_PLAN_PCT = "sections.coverage.plan_coinsurance"
_CPT_PCT = "sections.coverage.svc.cpt_1.coinsurance"
_CPT_COPAY = "sections.coverage.svc.cpt_1.copay"


@cache
def _migration_module() -> ModuleType:
    """Load the migration by path so the test exercises its own helpers. Cached — every
    call site below would otherwise re-exec the module."""
    spec = importlib.util.spec_from_file_location("migration_percent_backfill", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_backfill(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Mirrors upgrade()'s loop exactly, reusing the migration's own SQL and helpers.
    Returns how many rows it rewrote, so idempotency is observable."""
    module = _migration_module()
    written = 0
    async with sessionmaker() as session, session.begin():
        privileged = bool((await session.execute(text(module.PRIVILEGE_SQL))).scalar())
        module.abort_if_rls_would_hide_rows(privileged)
        versions = (await session.execute(text(module.SELECT_SCHEMA_VERSIONS_SQL))).all()
        for version_id, schema_json_text in versions:
            paths = module.percent_leaf_paths(json.loads(schema_json_text))
            if not paths:
                continue
            after = UUID(int=0)
            while rows := (
                await session.execute(
                    text(module.SELECT_ANSWERS_SQL),
                    {
                        "schema_version_id": version_id,
                        "paths": paths,
                        "after": after,
                        "limit": module._CHUNK,
                    },
                )
            ).all():
                after = rows[-1][0]
                updates = [
                    {"id": row_id, "new": canonical}
                    for row_id, stored in rows
                    if (canonical := module.canonical_percent(stored)) is not None
                    and canonical != stored
                ]
                if updates:
                    await session.execute(text(module.UPDATE_ANSWER_SQL), updates)
                    written += len(updates)
    return written


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Remove this fixture's whole world, in FK order.

    Forms/answers are keyed off the fixture TENANT rather than off the schema, so a
    partially-torn-down previous run (schema already gone, tenant still there) is still
    cleaned up — the tenant slug is UNIQUE, so a surviving row breaks the next setup.
    `form_schema.insurance_type` is globally unique, hence the by-insurance_type wipe;
    prompt_version.schema_version_id is RESTRICT, so Prompts go before SchemaVersions."""
    async with sessionmaker() as session, session.begin():
        tenant_ids = (
            (await session.execute(select(Tenant.id).where(Tenant.slug == _TENANT_SLUG)))
            .scalars()
            .all()
        )
        if tenant_ids:
            await session.execute(delete(FieldAnswer).where(FieldAnswer.tenant_id.in_(tenant_ids)))
            await session.execute(delete(PatientForm).where(PatientForm.tenant_id.in_(tenant_ids)))
        schema_ids = (
            (await session.execute(select(FormSchema.id).where(FormSchema.insurance_type == IBV)))
            .scalars()
            .all()
        )
        if schema_ids:
            await session.execute(delete(Prompt).where(Prompt.schema_id.in_(schema_ids)))
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids))
            )
            await session.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))
        if tenant_ids:
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))


@pytest.fixture
async def percent_world(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, UUID]]:
    await _wipe(admin_sessionmaker)
    async with admin_sessionmaker() as session, session.begin():
        tenant = Tenant(name="Percent Backfill Fixture", slug=_TENANT_SLUG)
        schema = FormSchema(insurance_type=IBV, name="Percent Backfill Fixture")
        session.add_all([tenant, schema])
        await session.flush()

        v2 = SchemaVersion(
            schema_id=schema.id, version=1, schema_json=V2_DOC, status=VersionStatus.DRAFT
        )
        v1 = SchemaVersion(
            schema_id=schema.id, version=2, schema_json=V1_DOC, status=VersionStatus.DRAFT
        )
        session.add_all([v2, v1])
        await session.flush()

        v2_form = PatientForm(tenant_id=tenant.id, schema_version_id=v2.id)
        v1_form = PatientForm(tenant_id=tenant.id, schema_version_id=v1.id)
        session.add_all([v2_form, v1_form])
        await session.flush()

        def answer(
            form: PatientForm,
            path: str,
            value: Any,
            *,
            current: bool = True,
            source: str = "ai_call",
        ) -> FieldAnswer:
            row = FieldAnswer(
                tenant_id=tenant.id,
                form_id=form.id,
                field_path=path,
                value={"value": value},
                source=source,
                is_current=current,
            )
            session.add(row)
            return row

        rows = {
            "bare": answer(v2_form, _PLAN_PCT, "20"),
            # A superseded human baseline: `baseline_value` still resolves it, so it MUST
            # be rewritten or it becomes a false dispute against the canonical AI answer.
            "history": answer(
                v2_form,
                _CPT_PCT,
                "30",
                current=False,
                source=AnswerSource.HUMAN.value,
            ),
            "already_canonical": answer(v2_form, _CPT_PCT, "0%"),
            "currency_sibling": answer(v2_form, _CPT_COPAY, "20"),
            "v1_untouched": answer(v1_form, _PLAN_PCT, "20"),
        }
        await session.flush()
        ids = {name: row.id for name, row in rows.items()}
    yield ids
    await _wipe(admin_sessionmaker)


async def _value(sessionmaker: async_sessionmaker[AsyncSession], answer_id: UUID) -> Any:
    async with sessionmaker() as session:
        return (
            await session.execute(select(FieldAnswer.value).where(FieldAnswer.id == answer_id))
        ).scalar_one()


class TestPercentLeafPaths:
    def test_walks_nested_groups_and_skips_the_fields_container_key(self) -> None:
        assert sorted(_migration_module().percent_leaf_paths(V2_DOC)) == [_PLAN_PCT, _CPT_PCT]

    def test_ignores_a_document_with_no_sections(self) -> None:
        assert _migration_module().percent_leaf_paths(V1_DOC) == []


class TestCanonicalPercent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("20", "20%"),
            ("20%", "20%"),
            (" 20 %", "20%"),
            ("20 percent", "20%"),
            ("020", "20%"),
            ("12.50", "12.5%"),
            ("0", "0%"),
        ],
    )
    def test_canonicalizes(self, raw: str, expected: str) -> None:
        assert _migration_module().canonical_percent(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "N/A", "20% after deductible", "twenty percent"])
    def test_returns_none_for_anything_it_does_not_recognize(self, raw: str) -> None:
        """None means "leave the row exactly as it is" — never guess, never blank a value."""
        assert _migration_module().canonical_percent(raw) is None

    def test_does_not_rescale_a_fraction(self) -> None:
        assert _migration_module().canonical_percent("0.2") == "0.2%"


class TestPrivilegeGuard:
    def test_allows_a_privileged_role(self) -> None:
        _migration_module().abort_if_rls_would_hide_rows(True)

    def test_aborts_without_superuser_or_bypassrls(self) -> None:
        with pytest.raises(RuntimeError, match="BYPASSRLS"):
            _migration_module().abort_if_rls_would_hide_rows(False)

    async def test_the_real_non_superuser_role_would_be_rejected(
        self, rls_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The guard exists because FORCE RLS on `field_answer` makes an unprivileged
        migration a silent zero-row no-op. Prove the app-shaped role really does fail it."""
        module = _migration_module()
        async with rls_sessionmaker() as session:
            privileged = bool((await session.execute(text(module.PRIVILEGE_SQL))).scalar())
        assert privileged is False
        with pytest.raises(RuntimeError, match="BYPASSRLS"):
            module.abort_if_rls_would_hide_rows(privileged)


class TestBackfill:
    async def test_rewrites_bare_numbers_including_non_current_history(
        self,
        percent_world: dict[str, UUID],
        admin_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _run_backfill(admin_sessionmaker)

        assert await _value(admin_sessionmaker, percent_world["bare"]) == {"value": "20%"}
        # The superseded baseline — the row that would otherwise fake a dispute.
        assert await _value(admin_sessionmaker, percent_world["history"]) == {"value": "30%"}

    async def test_leaves_already_canonical_currency_and_v1_rows_alone(
        self,
        percent_world: dict[str, UUID],
        admin_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _run_backfill(admin_sessionmaker)

        assert await _value(admin_sessionmaker, percent_world["already_canonical"]) == {
            "value": "0%"
        }
        # Keyed on leaf.type, so the currency sibling keeps its (separately buggy) shape.
        assert await _value(admin_sessionmaker, percent_world["currency_sibling"]) == {
            "value": "20"
        }
        # v1 has no percent leaf and is excluded by the dsl_version predicate.
        assert await _value(admin_sessionmaker, percent_world["v1_untouched"]) == {"value": "20"}

    async def test_re_running_is_a_no_op(
        self,
        percent_world: dict[str, UUID],
        admin_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """CI runs `alembic upgrade head` from 0001, and the dev DB may be migrated twice."""
        first = await _run_backfill(admin_sessionmaker)
        second = await _run_backfill(admin_sessionmaker)

        assert first > 0  # the fixture really did have work to do
        assert second == 0
