# Dynamic Voice Cascade LLM Model Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a platform superadmin override the voice cascade's Gemini LLM model name at runtime from a new admin page, with a DB shape general enough to later cover STT/TTS and provider changes without a redesign.

**Architecture:** A new global, append-only `voice_model_config` table (no `tenant_id`, no RLS) holds one row per save/reset; the newest row per `stage` is the current effective value. New `platform:llm_config:read`/`:write`-gated control_plane endpoints read/write it. At call-dispatch time (both `queue_dispatcher.py` and `voice_lab.py`), a shared helper reads the active `llm` row and adds it to the LiveKit job dispatch `metadata` — the same channel `persona_tweak` already uses to reach the DB-free agent worker. `cascade.py`'s `build_session` accepts the override and falls back to a named default constant. A new superuser-only frontend page (pattern-matched off `InsuranceProviders.tsx`) exposes save/reset/history.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (async) / Alembic / pytest (backend, `vera-backend/`, uv workspace); React 19 / TypeScript / Vitest / shadcn-Radix / Redux Toolkit (frontend, `vera-frontend/`).

## Global Constraints

- Scope: **LLM model name only, Google provider only, platform-wide (global, not per-tenant)** this iteration. No provider switching. The DB `stage` column must already admit `stt`/`llm`/`tts` even though only `llm` is written/read this iteration.
- Freeform model name accepted. Suggested values (UI quick-picks only, not enforced): `gemini-2.5-flash`, `gemini-3.1-flash-lite`, `gemini-3.5-flash`, `gemini-3.6-flash`.
- **No live Vertex AI validation** — control_plane's prod service account has no `aiplatform.*` IAM grant yet (`adr/devops-todo.md` row 13, still open). Validation is basic only: trimmed, non-empty, max 200 chars, charset `[A-Za-z0-9._-]+`.
- **Append-only history, no `is_active` flag** — every save/reset is a new INSERT, never an UPDATE. "Current" = newest row per `stage` (`ORDER BY created_at DESC, id DESC LIMIT 1`). A row with `model IS NULL` (paired with `provider IS NULL`) is the explicit "reset to default" state.
- Every new migration must be idempotent (`CREATE TABLE IF NOT EXISTS`, guarded `ADD CONSTRAINT`) — `migrations/0001_initial.py` runs `Base.metadata.create_all()` against **current** models at runtime, so a fresh DB already has anything currently in the models by the time a later migration runs; only an already-provisioned DB needs the explicit migration.
- Migration revision IDs are alembic's random hex, generated via `just makemigration "<message>"` (never hand-numbered).
- Backend command prefix: run all `just`/`uv` commands from the `vera-backend/` directory. Frontend commands from `vera-frontend/`.
- Backend final gate for every task: `just check` (= `ruff format --check` + `ruff check` + `mypy` + `pytest`, per `justfile`).
- Frontend final gate for every task touching `vera-frontend/`: `npx tsc -b && npx eslint . && npm test && npm run build`.
- Per repo-root `CLAUDE.md` (Vera 2.0 repo-wide rule): after all implementation tasks are complete, run the **code-simplifier** agent on the full diff in the same session, then re-run both gates above, before considering the work done — this is Task 10 below.
- Do not add `Co-Authored-By: Claude` to any commit message (user's global instruction).
- PHI: this feature touches no PHI. The dispatch `metadata` dict already carries PHI elsewhere (`agent_context`) — never log the full `metadata` dict; only the specific key this feature adds is safe to log if ever needed.

---

### Task 1: `VoiceModelConfig` model + table migration

**Files:**
- Create: `packages/vera_core/src/vera_core/models/voice_model_config.py`
- Modify: `packages/vera_core/src/vera_core/models/enums.py` (add `VoiceModelStage`)
- Modify: `packages/vera_core/src/vera_core/models/__init__.py` (register the model)
- Create: `migrations/versions/<generated>_create_voice_model_config.py`
- Test: `tests/integration/db/test_voice_model_config.py`

**Interfaces:**
- Produces: `VoiceModelConfig` (SQLAlchemy model, columns `id: UUID`, `stage: str`, `provider: str | None`, `model: str | None`, `created_by_user_id: UUID | None`, `created_at: datetime`), importable as `from vera_core.models import VoiceModelConfig`. `VoiceModelStage` (StrEnum: `STT = "stt"`, `LLM = "llm"`, `TTS = "tts"`), importable as `from vera_core.models.enums import VoiceModelStage`.

- [ ] **Step 1: Add `VoiceModelStage` to `enums.py`**

Open `packages/vera_core/src/vera_core/models/enums.py` and add, near `ProviderStatus`/`PlaybookStatus` (both defined around line 166-181):

```python
class VoiceModelStage(enum.StrEnum):
    """Which voice-cascade stage a `voice_model_config` row overrides. Only LLM is
    read by the cascade today; STT/TTS are schema-ready for a future iteration."""

    STT = "stt"
    LLM = "llm"
    TTS = "tts"
```

- [ ] **Step 2: Write the model**

Create `packages/vera_core/src/vera_core/models/voice_model_config.py`:

```python
"""Global, append-only log of voice-cascade model overrides (GLOBAL: no tenant_id, no
RLS, no PHI) — a platform surface a SUPER_ADMIN curates, mirroring Prompt/PromptVersion.

Never updated in place: every save or reset is a new row (CreatedAtMixin, no `is_active`
flag). The newest row per `stage` IS the current effective value — `model IS NULL`
(paired with `provider IS NULL`) is the explicit "reset to default" state, a real
queryable row rather than an absence of rows. Only `stage == VoiceModelStage.LLM` is
written today; STT/TTS are schema-ready for a future iteration (see agent_worker/cascade.py).
"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, CreatedAtMixin, UUIDv7PKMixin
from vera_core.models.enums import VoiceModelStage, check_in


class VoiceModelConfig(Base, UUIDv7PKMixin, CreatedAtMixin):
    __tablename__ = "voice_model_config"

    __table_args__ = (
        check_in("stage", VoiceModelStage),
        CheckConstraint(
            "(model IS NULL AND provider IS NULL) OR (model IS NOT NULL AND provider IS NOT NULL)",
            name="model_provider_pair",
        ),
        Index("ix_voice_model_config_stage_created_at", "stage", "created_at"),
    )

    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )
```

- [ ] **Step 3: Register the model**

In `packages/vera_core/src/vera_core/models/__init__.py`, add the import after the `.tenant` import and before `.transcript`:

```python
from .tenant import Tenant
from .transcript import Recording, Transcript
from .voice_model_config import VoiceModelConfig
```

(Reorder if needed so imports stay roughly alphabetical — `voice_model_config` sorts after `transcript`, so append it last.) Add `"VoiceModelConfig",` to `__all__`, alphabetically after `"UserRole"`.

- [ ] **Step 4: Generate the migration scaffold**

Run: `just makemigration "create voice_model_config table"`

This creates `migrations/versions/<date>_<time>_<hex>_create_voice_model_config.py` with an autogenerated `upgrade()`/`downgrade()` body (likely `op.create_table(...)`) and the correct `revision`/`down_revision` already filled in from the current head. **Do not keep the autogenerated body** — replace `upgrade()`/`downgrade()` per the next step (autogenerated `op.create_table` is not idempotent and will fail CI's from-scratch migration run once `0001` also creates this table for a fresh DB).

- [ ] **Step 5: Replace the migration body with the idempotent form**

Open the generated file and replace its docstring and `upgrade()`/`downgrade()` (keep the autogenerated `revision`/`down_revision`/`branch_labels`/`depends_on` values exactly as generated):

```python
"""create voice_model_config table

Global, append-only log of voice-cascade model overrides
(packages/vera_core/src/vera_core/models/voice_model_config.py) — no tenant_id, no RLS,
mirrors Prompt/FormSchema. Also lives in the models, so migration 0001 materializes it
for a FRESH DB — this migration only provisions it on an EXISTING DB (mirrors
0e78b863d8a3_ivr_playbook_and_insurance_provider_...'s idempotent posture: guarded
CREATE TABLE / ADD CONSTRAINT, safe to run even where the objects already exist).
"""

from collections.abc import Sequence

from alembic import op

# Keep the revision/down_revision/branch_labels/depends_on values `just makemigration`
# generated above this line — do not hand-edit them.


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_model_config (
            id UUID PRIMARY KEY,
            stage VARCHAR(16) NOT NULL,
            provider VARCHAR(64),
            model VARCHAR(200),
            created_by_user_id UUID REFERENCES app_user(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_voice_model_config_stage_valid'
            ) THEN
                ALTER TABLE voice_model_config ADD CONSTRAINT ck_voice_model_config_stage_valid
                    CHECK (stage IN ('stt', 'llm', 'tts'));
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_voice_model_config_model_provider_pair'
            ) THEN
                ALTER TABLE voice_model_config
                    ADD CONSTRAINT ck_voice_model_config_model_provider_pair
                    CHECK (
                        (model IS NULL AND provider IS NULL)
                        OR (model IS NOT NULL AND provider IS NOT NULL)
                    );
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_voice_model_config_stage_created_at "
        "ON voice_model_config (stage, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_model_config")
```

(Keep the `revision`, `down_revision`, `branch_labels`, `depends_on`, and `Create Date` lines exactly as `just makemigration` generated them — only the docstring body above `revision =` and the two functions are replaced. `Sequence` import may be unused now depending on the generated header — remove it if `ruff` flags it unused.)

- [ ] **Step 6: Apply the migration**

Run: `just migrate`

Expected: no errors; `voice_model_config` now exists in the local dev DB.

- [ ] **Step 7: Write the failing integration test**

Create `tests/integration/db/test_voice_model_config.py`:

```python
"""DB-level coverage for voice_model_config: the stage / model-provider-pair CHECK
constraints, and "current effective value = newest row per stage" query behavior.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.models import VoiceModelConfig


@pytest.fixture
async def sm(database_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def cleanup(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[None]:
    yield
    async with sm() as s, s.begin():
        await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_rejects_unknown_stage(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="bogus", provider="google", model="gemini-2.5-flash"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_rejects_model_without_provider(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider=None, model="gemini-2.5-flash"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_allows_explicit_reset_row(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider=None, model=None))
        await s.flush()  # no IntegrityError — both-null is the valid "reset" pairing


async def test_newest_row_per_stage_is_the_current_value(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider="google", model="gemini-2.5-flash"))
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider="google", model="gemini-3.5-flash"))

    async with sm() as s:
        current = (
            await s.execute(
                select(VoiceModelConfig)
                .where(VoiceModelConfig.stage == "llm")
                .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert current.model == "gemini-3.5-flash"
```

- [ ] **Step 8: Run the test**

Run: `just test tests/integration/db/test_voice_model_config.py -v`
Expected: all 4 tests PASS (requires local Postgres — `just up` first if not already running).

- [ ] **Step 9: Full backend gate + commit**

Run: `just check`
Expected: PASS.

```bash
git add packages/vera_core/src/vera_core/models/voice_model_config.py \
        packages/vera_core/src/vera_core/models/enums.py \
        packages/vera_core/src/vera_core/models/__init__.py \
        migrations/versions/ \
        tests/integration/db/test_voice_model_config.py
git commit -m "feat: add voice_model_config table for runtime model overrides"
```

---

### Task 2: Permissions catalog + seed migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Modify: `tests/unit/test_rbac_defaults.py`
- Create: `migrations/versions/<generated>_seed_llm_config_permissions.py`

**Interfaces:**
- Consumes: none (independent of Task 1).
- Produces: two new permission codes, `"platform:llm_config:read"` and `"platform:llm_config:write"`, present in `PLATFORM_PERMISSIONS` and granted to `SYSTEM_ROLES["SUPER_ADMIN"]` — consumed by Task 4's `platform_require(...)` calls.

- [ ] **Step 1: Write the failing unit test**

In `tests/unit/test_rbac_defaults.py`, add (after `test_form_schemas_read_permission_is_catalogued_and_super_admin_only`):

```python
def test_llm_config_permissions_are_catalogued_and_super_admin_only() -> None:
    for code in ("platform:llm_config:read", "platform:llm_config:write"):
        assert code in PLATFORM_PERMISSIONS
        assert code in SYSTEM_ROLES["SUPER_ADMIN"]
        assert code not in SYSTEM_ROLES["TENANT_ADMIN"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/unit/test_rbac_defaults.py::test_llm_config_permissions_are_catalogued_and_super_admin_only -v`
Expected: FAIL (`KeyError`/`AssertionError` — the codes don't exist yet).

- [ ] **Step 3: Add the permissions**

In `packages/vera_core/src/vera_core/models/rbac_defaults.py`, add to `PLATFORM_PERMISSIONS` (after `"platform:form_schemas:read"`):

```python
    "platform:form_schemas:read": "View form schemas and their versions",
    "platform:llm_config:read": "View the active voice cascade LLM model override",
    "platform:llm_config:write": "Set or reset the voice cascade LLM model override",
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `just test tests/unit/test_rbac_defaults.py -v`
Expected: all PASS.

- [ ] **Step 5: Generate and write the seed migration**

Run: `just makemigration "seed llm_config permissions"`

Replace the generated file's docstring and `upgrade()`/`downgrade()` (keep the generated `revision`/`down_revision`):

```python
"""seed platform:llm_config:read/write permissions and grant to SUPER_ADMIN

The new Super Admin "Voice Model" page needs its own platform permissions, gating
api/v1/llm_config.py. Mirrors f503e82734cc_seed_form_schemas_read_permission.py — two
new capabilities, not a rename/backfill.
"""

from alembic import op

_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("platform:llm_config:read", "View the active voice cascade LLM model override"),
    ("platform:llm_config:write", "Set or reset the voice cascade LLM model override"),
)


def upgrade() -> None:
    for code, description in _PERMISSIONS:
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{description}') "
            "ON CONFLICT (code) DO NOTHING"
        )
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id "
            "FROM role r, permission p "
            f"WHERE r.tenant_id IS NULL AND r.name = 'SUPER_ADMIN' AND p.code = '{code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Same rationale as the other permission seeds: grants are indistinguishable from
    # live product data added since — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_llm_config_permissions: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
```

- [ ] **Step 6: Apply and verify**

Run: `just migrate`
Expected: no errors.

- [ ] **Step 7: Full backend gate + commit**

Run: `just check`

```bash
git add packages/vera_core/src/vera_core/models/rbac_defaults.py \
        tests/unit/test_rbac_defaults.py \
        migrations/versions/
git commit -m "feat: seed platform:llm_config permissions for SUPER_ADMIN"
```

---

### Task 3: `model_config.py` service module

**Files:**
- Create: `packages/vera_core/src/vera_core/services/model_config.py`
- Test: `tests/unit/test_model_config.py`
- Test: `tests/integration/db/test_model_config_service.py`

**Interfaces:**
- Consumes: `VoiceModelConfig`, `VoiceModelStage` from Task 1.
- Produces (all importable from `vera_core.services.model_config`):
  - `class InvalidModelName(ValueError)`
  - `def normalize_model_name(raw: str) -> str` (raises `InvalidModelName`)
  - `async def get_active_llm_config(session: AsyncSession) -> VoiceModelConfig | None`
  - `async def list_llm_config_history(session: AsyncSession, *, limit: int = 50) -> Sequence[VoiceModelConfig]`
  - `async def save_llm_model(session: AsyncSession, raw_model: str, *, created_by_user_id: UUID | None) -> VoiceModelConfig`
  - `async def reset_llm_model(session: AsyncSession, *, created_by_user_id: UUID | None) -> VoiceModelConfig | None` (returns `None` when already at default — no row inserted)
  - `async def add_llm_model_override_metadata(session: AsyncSession, metadata: dict[str, Any]) -> None` — consumed by Task 5.

- [ ] **Step 1: Write the failing unit test for `normalize_model_name`**

Create `tests/unit/test_model_config.py`:

```python
import pytest

from vera_core.services.model_config import InvalidModelName, normalize_model_name


def test_trims_surrounding_whitespace() -> None:
    assert normalize_model_name("  gemini-3.5-flash  ") == "gemini-3.5-flash"


def test_rejects_empty() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("")


def test_rejects_whitespace_only() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("   ")


def test_rejects_too_long() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("g" * 201)


def test_accepts_max_length() -> None:
    name = "g" * 200
    assert normalize_model_name(name) == name


def test_rejects_disallowed_characters() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("gemini 3.5 flash")  # spaces not allowed


def test_accepts_dots_hyphens_underscores() -> None:
    assert normalize_model_name("gemini-3.1_flash-lite") == "gemini-3.1_flash-lite"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/unit/test_model_config.py -v`
Expected: FAIL (`ModuleNotFoundError: vera_core.services.model_config`).

- [ ] **Step 3: Write the service module**

Create `packages/vera_core/src/vera_core/services/model_config.py`:

```python
"""Voice-cascade model override — currently LLM (Google) only; the table generalizes to
STT/TTS for a future iteration. Global, append-only: `get_active_llm_config` and
`list_llm_config_history` only read `voice_model_config`; `save_llm_model` and
`reset_llm_model` only ever INSERT, mirroring VoiceModelConfig's append-only shape.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import VoiceModelConfig
from vera_core.models.enums import VoiceModelStage

logger = logging.getLogger(__name__)

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_MODEL_NAME_LENGTH = 200
_LLM_PROVIDER = "google"


class InvalidModelName(ValueError):
    pass


def normalize_model_name(raw: str) -> str:
    """Trim + validate a freeform model name. Deliberately permissive charset (letters,
    digits, dot, hyphen, underscore) — covers every real Gemini model id — while still
    rejecting empty/whitespace-only input and anything absurdly long. No live Vertex AI
    check (control_plane has no aiplatform IAM grant yet — adr/devops-todo.md)."""
    trimmed = raw.strip()
    if not trimmed:
        raise InvalidModelName("model name must not be empty")
    if len(trimmed) > _MAX_MODEL_NAME_LENGTH:
        raise InvalidModelName(f"model name must be at most {_MAX_MODEL_NAME_LENGTH} characters")
    if not _MODEL_NAME_RE.match(trimmed):
        raise InvalidModelName("model name may only contain letters, digits, '.', '-', '_'")
    return trimmed


async def get_active_llm_config(session: AsyncSession) -> VoiceModelConfig | None:
    """The newest voice_model_config row for the llm stage, or None if never written.
    Either way, `model is None` (row or no row) means "use the hardcoded default"."""
    return (
        await session.execute(
            select(VoiceModelConfig)
            .where(VoiceModelConfig.stage == VoiceModelStage.LLM)
            .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_llm_config_history(
    session: AsyncSession, *, limit: int = 50
) -> Sequence[VoiceModelConfig]:
    return (
        (
            await session.execute(
                select(VoiceModelConfig)
                .where(VoiceModelConfig.stage == VoiceModelStage.LLM)
                .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def save_llm_model(
    session: AsyncSession, raw_model: str, *, created_by_user_id: UUID | None
) -> VoiceModelConfig:
    row = VoiceModelConfig(
        stage=VoiceModelStage.LLM,
        provider=_LLM_PROVIDER,
        model=normalize_model_name(raw_model),
        created_by_user_id=created_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row


async def reset_llm_model(
    session: AsyncSession, *, created_by_user_id: UUID | None
) -> VoiceModelConfig | None:
    """No-op (returns None) if already at default; otherwise inserts an explicit reset
    row (provider/model both NULL) so history shows who cleared it and when."""
    current = await get_active_llm_config(session)
    if current is None or current.model is None:
        return None
    row = VoiceModelConfig(
        stage=VoiceModelStage.LLM,
        provider=None,
        model=None,
        created_by_user_id=created_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row


async def add_llm_model_override_metadata(session: AsyncSession, metadata: dict[str, Any]) -> None:
    """Mirrors add_active_playbook_metadata's missing-key convention: a missing
    `llm_model_override` key means "use the hardcoded cascade default". A broken config
    table must never block a call from being placed, so any read failure degrades to the
    same missing-key default rather than propagating and failing the whole dispatch."""
    try:
        current = await get_active_llm_config(session)
    except Exception as exc:
        logger.warning("llm model override lookup failed (%s) — using default", type(exc).__name__)
        return
    if current is not None and current.model:
        metadata["llm_model_override"] = current.model
```

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `just test tests/unit/test_model_config.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Write the failing integration test for the DB-backed functions**

Create `tests/integration/db/test_model_config_service.py`:

```python
"""DB-backed coverage for model_config.py's get/save/reset/history and the dispatch
metadata helper — against real Postgres.
"""

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.models import VoiceModelConfig
from vera_core.services import model_config
from vera_core.services.model_config import (
    add_llm_model_override_metadata,
    get_active_llm_config,
    list_llm_config_history,
    reset_llm_model,
    save_llm_model,
)


@pytest.fixture
async def sm(database_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def cleanup(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[None]:
    yield
    async with sm() as s, s.begin():
        await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_get_active_returns_none_when_never_set(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s:
        assert await get_active_llm_config(s) is None


async def test_save_then_get_active_returns_the_saved_row(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s, s.begin():
        saved = await save_llm_model(s, " gemini-3.5-flash ", created_by_user_id=None)
        assert saved.model == "gemini-3.5-flash"
        assert saved.provider == "google"

    async with sm() as s:
        current = await get_active_llm_config(s)
        assert current is not None
        assert current.model == "gemini-3.5-flash"


async def test_reset_when_never_set_is_a_noop(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        assert await reset_llm_model(s, created_by_user_id=None) is None


async def test_reset_after_save_clears_and_history_shows_it(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s, s.begin():
        await save_llm_model(s, "gemini-3.5-flash", created_by_user_id=None)
    async with sm() as s, s.begin():
        reset_row = await reset_llm_model(s, created_by_user_id=None)
        assert reset_row is not None
        assert reset_row.model is None

    async with sm() as s:
        assert await get_active_llm_config(s) is None  # newest row has model=None
        history = await list_llm_config_history(s)
        assert len(history) == 2
        assert history[0].model is None  # newest first: the reset
        assert history[1].model == "gemini-3.5-flash"


async def test_add_llm_model_override_metadata_sets_key_when_active(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s, s.begin():
        await save_llm_model(s, "gemini-3.6-flash", created_by_user_id=None)

    async with sm() as s:
        metadata: dict[str, object] = {}
        await add_llm_model_override_metadata(s, metadata)
        assert metadata == {"llm_model_override": "gemini-3.6-flash"}


async def test_add_llm_model_override_metadata_omits_key_when_unset(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s:
        metadata: dict[str, object] = {}
        await add_llm_model_override_metadata(s, metadata)
        assert metadata == {}


async def test_add_llm_model_override_metadata_degrades_to_default_on_read_failure(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    # A broken config-table read must never block a call from being placed — it degrades
    # to the same missing-key ("use the hardcoded default") behavior, not an exception.
    async with sm() as s:
        metadata: dict[str, object] = {}
        with patch.object(model_config, "get_active_llm_config", side_effect=RuntimeError("boom")):
            await add_llm_model_override_metadata(s, metadata)
        assert metadata == {}
```

- [ ] **Step 6: Run it to verify it fails first, then passes**

Run: `just test tests/integration/db/test_model_config_service.py -v`
Expected: (before Step 3's module existed this would fail on import; since Step 3 is already done, this should PASS immediately.) Confirm all 7 PASS.

- [ ] **Step 7: Full backend gate + commit**

Run: `just check`

```bash
git add packages/vera_core/src/vera_core/services/model_config.py \
        tests/unit/test_model_config.py \
        tests/integration/db/test_model_config_service.py
git commit -m "feat: add model_config service for LLM override get/save/reset/history"
```

---

### Task 4: control_plane endpoints

**Files:**
- Create: `apps/control_plane/src/control_plane/api/v1/llm_config.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/__init__.py`
- Test: `tests/integration/control_plane/test_llm_config.py`

**Interfaces:**
- Consumes: from Task 2, permissions `"platform:llm_config:read"`/`"platform:llm_config:write"`; from Task 3, `get_active_llm_config`, `list_llm_config_history`, `save_llm_model`, `reset_llm_model`, `InvalidModelName`.
- Produces: `GET /api/v1/platform/llm-config`, `GET /api/v1/platform/llm-config/history`, `PUT /api/v1/platform/llm-config` (body `{"model": str}`), `POST /api/v1/platform/llm-config/reset`. Response shape `LlmConfigState = {provider: str|None, model: str|None, is_default: bool, created_at: str|None, created_by_user_id: str|None}` — this exact shape is what Task 7's frontend API layer types against.

- [ ] **Step 1: Write the router**

Create `apps/control_plane/src/control_plane/api/v1/llm_config.py`:

```python
"""Platform (SUPER_ADMIN) voice-cascade LLM model override.

Global (no tenant_id, no RLS) — a platform surface a SUPER_ADMIN curates, applying to
every tenant's calls. Basic validation only (see vera_core.services.model_config); no
live Vertex AI check (control_plane's prod service account has no aiplatform IAM grant
yet — see adr/devops-todo.md). Delivery to the DB-free agent worker happens separately,
at dispatch time (queue_dispatcher.py / voice_lab.py), not from this router.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import get_idempotency_store, platform_scoped_session
from control_plane.exceptions import CustomAPIException, CustomAPIResponse, DefaultExceptionCode
from control_plane.idempotency import (
    PLATFORM_IDEM_SCOPE,
    claim_or_conflict,
    require_idempotency_key,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import VoiceModelConfig
from vera_core.services.model_config import (
    InvalidModelName,
    get_active_llm_config,
    list_llm_config_history,
    reset_llm_model,
    save_llm_model,
)

router = APIRouter(prefix="/platform/llm-config", tags=["platform-llm-config"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:llm_config:read")
_WRITE = platform_require("platform:llm_config:write")


class SaveLlmConfigRequest(BaseModel):
    model: str


class LlmConfigState(BaseModel):
    provider: str | None
    model: str | None
    is_default: bool
    created_at: datetime | None
    created_by_user_id: UUID | None


def _state(row: VoiceModelConfig | None) -> LlmConfigState:
    if row is None:
        return LlmConfigState(
            provider=None, model=None, is_default=True, created_at=None, created_by_user_id=None
        )
    return LlmConfigState(
        provider=row.provider,
        model=row.model,
        is_default=row.model is None,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


@router.get(
    "",
    response_model=ResponseModel[LlmConfigState],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def get_llm_config(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[LlmConfigState]:
    return ok(_state(await get_active_llm_config(session)))


@router.get(
    "/history",
    response_model=ResponseModel[list[LlmConfigState]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def get_llm_config_history(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[LlmConfigState]]:
    rows = await list_llm_config_history(session)
    return ok([_state(row) for row in rows])


@router.put(
    "",
    response_model=ResponseModel[LlmConfigState],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def save_llm_config(
    body: SaveLlmConfigRequest,
    request: Request,
    session: PlatformSession,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[LlmConfigState]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    try:
        row = await save_llm_model(session, body.model, created_by_user_id=caller.user_id)
    except InvalidModelName as exc:
        raise CustomAPIException(DefaultExceptionCode.VALIDATION_ERROR, message=str(exc)) from exc
    return ok(_state(row))


@router.post(
    "/reset",
    response_model=ResponseModel[LlmConfigState],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def reset_llm_config(
    request: Request,
    session: PlatformSession,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[LlmConfigState]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    await reset_llm_model(session, created_by_user_id=caller.user_id)
    return ok(_state(await get_active_llm_config(session)))
```

- [ ] **Step 2: Register the router**

In `apps/control_plane/src/control_plane/api/v1/__init__.py`, add the import (alphabetically, after `ivr_playbooks_router`'s import):

```python
from control_plane.api.v1.ivr_playbooks import router as ivr_playbooks_router
from control_plane.api.v1.llm_config import router as llm_config_router
```

And add `router.include_router(llm_config_router)` alongside the other `include_router` calls (e.g. right after `router.include_router(ivr_playbooks_router)`).

- [ ] **Step 3: Write the failing HTTP integration test**

Create `tests/integration/control_plane/test_llm_config.py`:

```python
"""HTTP-level tests for the platform LLM-model-override endpoints. Mirrors
test_ivr_playbooks.py's self-contained `playbooks_world` pattern.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, Tenant, UserRole


@dataclass
class World:
    tenant_id: UUID
    super_token: str
    tenant_admin_token: str
    name_suffix: str


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write_headers(token: str) -> dict[str, str]:
    return {**_auth(token), "Idempotency-Key": str(uuid7())}


async def _mint(
    store: InMemorySessionStore, *, user_id: UUID, tenant_id: UUID | None, email: str
) -> str:
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="tenant" if tenant_id is not None else "platform",
            tenant_slug=str(tenant_id) if tenant_id is not None else None,
        ),
        3600,
        3600,
    )


@pytest.fixture
async def llm_config_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, World]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id, super_id, admin_id = uuid7(), uuid7(), uuid7()
    suffix = tenant_id.hex[:8]

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name=f"LC {suffix}", status="active"))
        await s.flush()
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        admin_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
            )
        ).scalar_one()
        s.add(
            AppUser(
                id=super_id,
                tenant_id=None,
                account_type="platform",
                email=f"lc-root-{suffix}@vera.example",
                name="Root",
                status="active",
            )
        )
        s.add(
            AppUser(
                id=admin_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email=f"lc-ta-{suffix}@tenant.example",
                name="TA",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=tenant_id, app_user_id=admin_id, role_id=admin_role))

    store = InMemorySessionStore()
    super_token = await _mint(
        store, user_id=super_id, tenant_id=None, email=f"lc-root-{suffix}@vera.example"
    )
    admin_token = await _mint(
        store, user_id=admin_id, tenant_id=tenant_id, email=f"lc-ta-{suffix}@tenant.example"
    )

    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=store,
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, World(tenant_id, super_token, admin_token, suffix)

    async with sm() as s, s.begin():
        await s.execute(text("DELETE FROM voice_model_config WHERE stage = 'llm'"))
        for tbl in ("audit_log", "auth_audit_log", "user_role", "role_permission", "role"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t").bindparams(t=tenant_id))
        await s.execute(text("DELETE FROM user_role WHERE app_user_id = :u").bindparams(u=super_id))
        await s.execute(
            text("DELETE FROM app_user WHERE id IN (:s, :a)").bindparams(s=super_id, a=admin_id)
        )
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await engine.dispose()


async def test_get_llm_config_defaults_when_never_set(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.get("/api/v1/platform/llm-config", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["is_default"] is True
    assert body["model"] is None


async def test_save_then_get_reflects_override(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    saved = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["model"] == "gemini-3.5-flash"
    assert saved.json()["data"]["is_default"] is False

    current = await client.get("/api/v1/platform/llm-config", headers=_auth(w.super_token))
    assert current.json()["data"]["model"] == "gemini-3.5-flash"


async def test_save_rejects_blank_model(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "   "},
    )
    assert resp.status_code == 422, resp.text


async def test_reset_clears_override_and_is_idempotent(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    reset = await client.post(
        "/api/v1/platform/llm-config/reset", headers=_write_headers(w.super_token)
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["is_default"] is True

    # Already at default — a second reset is a no-op success, not an error.
    again = await client.post(
        "/api/v1/platform/llm-config/reset", headers=_write_headers(w.super_token)
    )
    assert again.status_code == 200, again.text


async def test_history_lists_saves_and_resets_newest_first(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-2.5-flash"},
    )
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    history = await client.get("/api/v1/platform/llm-config/history", headers=_auth(w.super_token))
    assert history.status_code == 200, history.text
    models = [row["model"] for row in history.json()["data"]]
    assert models[:2] == ["gemini-3.5-flash", "gemini-2.5-flash"]


async def test_routes_forbidden_for_tenant(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.get("/api/v1/platform/llm-config", headers=_auth(w.tenant_admin_token))
    assert resp.status_code == 403


async def test_write_requires_idempotency_key(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_auth(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    assert resp.status_code == 400, resp.text
```

- [ ] **Step 4: Run the test**

Run: `just test tests/integration/control_plane/test_llm_config.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Full backend gate + commit**

Run: `just check`

```bash
git add apps/control_plane/src/control_plane/api/v1/llm_config.py \
        apps/control_plane/src/control_plane/api/v1/__init__.py \
        tests/integration/control_plane/test_llm_config.py
git commit -m "feat: add platform LLM-config CRUD endpoints"
```

---

### Task 5: Wire the override into call dispatch

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/voice_lab.py`
- Test: `tests/integration/control_plane/test_llm_model_dispatch.py`

**Interfaces:**
- Consumes: `add_llm_model_override_metadata` from Task 3.
- Produces: dispatch `metadata["llm_model_override"]` (a plain string) present on every LiveKit job dispatch (real calls via `queue_dispatcher.py` and Voice Lab sandbox calls via `voice_lab.py`) whenever an active override exists — consumed by Task 6's `main.py` change.

- [ ] **Step 1: Write the failing test (Voice Lab path)**

Create `tests/integration/control_plane/test_llm_model_dispatch.py`:

```python
"""Confirms the active LLM model override rides Voice Lab's dispatch metadata (mirrors
test_ivr_playbooks.py's runtime-selection tests for add_active_playbook_metadata).
"""

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import VoiceModelConfig
from vera_core.models.enums import VoiceModelStage

from .conftest import FakeLiveKit, RBACWorld


async def test_voice_lab_carries_active_llm_override_into_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(
            VoiceModelConfig(stage=VoiceModelStage.LLM, provider="google", model="gemini-3.5-flash")
        )
    try:
        resp = await client.post(
            "/api/v1/voice-lab/sessions",
            headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
            json={"mode": "browser", "enable_ivr_navigation": False},
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert meta["llm_model_override"] == "gemini-3.5-flash"
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_voice_lab_omits_llm_model_override_when_never_set(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"mode": "browser", "enable_ivr_navigation": False},
    )
    assert resp.status_code == 200, resp.text
    meta = fake_livekit.dispatch_metadata[-1]
    assert meta is not None
    assert "llm_model_override" not in meta
```

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/integration/control_plane/test_llm_model_dispatch.py -v`
Expected: FAIL on the first test (`llm_model_override` key absent — not wired yet); second test passes trivially (already true).

- [ ] **Step 3: Wire `voice_lab.py`**

In `apps/control_plane/src/control_plane/api/v1/voice_lab.py`, add the import (alongside the existing `from vera_core.services.ivr_selection import add_active_playbook_metadata`):

```python
from vera_core.services.ivr_selection import add_active_playbook_metadata
from vera_core.services.model_config import add_llm_model_override_metadata
```

And in `start_voice_session`, right after the existing `if body.enable_ivr_navigation: ...` block and before `await livekit.create_call_room(room_name, metadata=metadata)`:

```python
    if body.enable_ivr_navigation:
        await add_active_playbook_metadata(session, body.insurance_provider_id, metadata)
    await add_llm_model_override_metadata(session, metadata)
    await livekit.create_call_room(room_name, metadata=metadata)
```

- [ ] **Step 4: Wire `queue_dispatcher.py`**

In `packages/vera_core/src/vera_core/services/queue_dispatcher.py`, add the import alongside the existing `ivr_selection` import (find the line importing `add_active_playbook_metadata, add_agent_context_metadata` near the top of the file, around line 62-63) and add:

```python
from vera_core.services.model_config import add_llm_model_override_metadata
```

Then, inside `try_dispatch`'s per-form loop, right after the existing:

```python
                if form.ivr_navigation_enabled and provider is not None:
                    await add_active_playbook_metadata(session, provider.id, metadata)
                if form.ivr_navigation_enabled:
                    await add_agent_context_metadata(session, form, metadata)
```

add:

```python
                await add_llm_model_override_metadata(session, metadata)
```

(This line is unconditional — the override applies to every real call, not gated on `ivr_navigation_enabled`.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `just test tests/integration/control_plane/test_llm_model_dispatch.py -v`
Expected: both PASS.

- [ ] **Step 6: Full backend gate + commit**

Run: `just check`

```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        apps/control_plane/src/control_plane/api/v1/voice_lab.py \
        tests/integration/control_plane/test_llm_model_dispatch.py
git commit -m "feat: carry active LLM model override into call-dispatch metadata"
```

---

### Task 6: agent_worker — `cascade.py` + `main.py`

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/cascade.py`
- Modify: `apps/agent_worker/src/agent_worker/main.py`
- Modify: `apps/agent_worker/tests/unit/test_cascade.py`

**Interfaces:**
- Consumes: dispatch metadata key `"llm_model_override"` (a `str`, set by Task 5) — read via `ctx.job.metadata`.
- Produces: `resolve_llm_model(llm_model: str | None) -> str` and `build_session(..., llm_model: str | None = None)` in `cascade.py`.

- [ ] **Step 1: Write the failing test**

In `apps/agent_worker/tests/unit/test_cascade.py`, add (the file currently only imports `cascade_session_kwargs` at the top — add the new import there too):

```python
from agent_worker.cascade import cascade_session_kwargs, resolve_llm_model
```

And append:

```python
def test_resolve_llm_model_uses_override_when_set() -> None:
    assert resolve_llm_model("gemini-3.5-flash") == "gemini-3.5-flash"


def test_resolve_llm_model_falls_back_to_default_when_unset() -> None:
    assert resolve_llm_model(None) == "gemini-2.5-flash"


def test_resolve_llm_model_falls_back_on_empty_string() -> None:
    assert resolve_llm_model("") == "gemini-2.5-flash"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/agent_worker && uv run pytest tests/unit/test_cascade.py -v`
Expected: FAIL (`ImportError: cannot import name 'resolve_llm_model'`).

- [ ] **Step 3: Modify `cascade.py`**

In `apps/agent_worker/src/agent_worker/cascade.py`, add the constant and helper (after `_VAD_SILENCE_DURATION`), and update `build_session`'s signature and body:

```python
_VAD_SILENCE_DURATION = 0.4
_DEFAULT_LLM_MODEL = "gemini-2.5-flash"


def resolve_llm_model(llm_model: str | None) -> str:
    """The runtime override if set (non-empty), else the hardcoded cascade default."""
    return llm_model or _DEFAULT_LLM_MODEL
```

Update `build_session`:

```python
def build_session(
    vad: Any | None = None,
    *,
    key_terms: list[str] | None = None,
    llm_model: str | None = None,
) -> AgentSession[TakeoverState]:
    # The latch must exist from construction: agents read it before speaking or hanging up.
    return AgentSession(
        userdata=TakeoverState(),
        stt=deepgram.STTv2(
            model="flux-general-en", eager_eot_threshold=0.5, **stt_kwargs(key_terms)
        ),
        llm=google.LLM(
            model=resolve_llm_model(llm_model),
            vertexai=True,
            location="global",
            thinking_config=ThinkingConfig(thinking_budget=0),
        ),
        tts=cartesia.TTS(model="sonic-3.5", emotion=["confident"]),
        vad=vad if vad is not None else _build_vad(),
        **cascade_session_kwargs(turn_detector=EnglishModel()),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/agent_worker && uv run pytest tests/unit/test_cascade.py -v`
Expected: all PASS (9 tests: the 6 pre-existing + 3 new).

- [ ] **Step 5: Wire `main.py`**

In `apps/agent_worker/src/agent_worker/main.py`, change the `build_session(...)` call inside `entrypoint()` (currently):

```python
        session = build_session(
            vad=ctx.proc.userdata.get("vad"),
            key_terms=controller.plan.stt_key_terms if controller is not None else None,
        )
```

to:

```python
        session = build_session(
            vad=ctx.proc.userdata.get("vad"),
            key_terms=controller.plan.stt_key_terms if controller is not None else None,
            llm_model=meta.get("llm_model_override"),
        )
```

- [ ] **Step 6: Run the full agent_worker test suite**

Run: `cd apps/agent_worker && uv run pytest tests/ -v`
Expected: all PASS, no regressions.

- [ ] **Step 7: Full backend gate + commit**

Run: `just check` (from `vera-backend/`)

```bash
git add apps/agent_worker/src/agent_worker/cascade.py \
        apps/agent_worker/src/agent_worker/main.py \
        apps/agent_worker/tests/unit/test_cascade.py
git commit -m "feat: agent_worker honors the dispatched LLM model override"
```

---

### Task 7: Frontend API layer — `llmConfig.ts`

**Files:**
- Create: `vera-frontend/src/lib/api/llmConfig.ts`
- Test: `vera-frontend/src/lib/api/llmConfig.test.ts`

**Interfaces:**
- Consumes: `apiRequest`, `randomId` from `@/lib/api/client` (existing).
- Produces: `type LlmConfigState = { provider: string | null; model: string | null; is_default: boolean; created_at: string | null; created_by_user_id: string | null }` and `getLlmConfig()`, `getLlmConfigHistory()`, `saveLlmConfig(model: string)`, `resetLlmConfig()` — all `Promise`-returning, consumed by Task 8 and Task 9.

- [ ] **Step 1: Write the failing test**

Create `vera-frontend/src/lib/api/llmConfig.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    httpStatus: number
    errorCode: string | null
    constructor(httpStatus: number, errorCode: string | null, message: string) {
      super(message)
      this.name = "ApiError"
      this.httpStatus = httpStatus
      this.errorCode = errorCode
    }
  }
  return { apiRequest: vi.fn(), ApiError, randomId: () => "test-idempotency-key" }
})

import { apiRequest } from "@/lib/api/client"
import { getLlmConfig, getLlmConfigHistory, resetLlmConfig, saveLlmConfig } from "./llmConfig"

describe("llmConfig api client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("fetches the current effective config", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await getLlmConfig()
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config")
  })

  it("fetches history", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await getLlmConfigHistory()
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config/history")
  })

  it("PUTs the model with the conventional Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await saveLlmConfig("gemini-3.5-flash")
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config", {
      method: "PUT",
      body: { model: "gemini-3.5-flash" },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("POSTs reset with the conventional Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await resetLlmConfig()
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config/reset", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd vera-frontend && npx vitest run src/lib/api/llmConfig.test.ts`
Expected: FAIL (`Cannot find module './llmConfig'`).

- [ ] **Step 3: Write the API layer**

Create `vera-frontend/src/lib/api/llmConfig.ts`:

```ts
// Platform (super admin) voice-cascade LLM model override endpoints.
// Mirrors backend api/v1/llm_config.py.
import { apiRequest, randomId } from "@/lib/api/client"

export type LlmConfigState = {
  provider: string | null
  model: string | null
  is_default: boolean
  created_at: string | null
  created_by_user_id: string | null
}

export function getLlmConfig() {
  return apiRequest<LlmConfigState>("/platform/llm-config")
}

export function getLlmConfigHistory() {
  return apiRequest<LlmConfigState[]>("/platform/llm-config/history")
}

export function saveLlmConfig(model: string) {
  return apiRequest<LlmConfigState>("/platform/llm-config", {
    method: "PUT",
    body: { model },
    headers: { "Idempotency-Key": randomId() },
  })
}

export function resetLlmConfig() {
  return apiRequest<LlmConfigState>("/platform/llm-config/reset", {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
  })
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd vera-frontend && npx vitest run src/lib/api/llmConfig.test.ts`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/api/llmConfig.ts vera-frontend/src/lib/api/llmConfig.test.ts
git commit -m "feat: add llmConfig API client"
```

---

### Task 8: Frontend pure helpers — `llmConfig.helpers.ts`

**Files:**
- Create: `vera-frontend/src/pages/llmConfig.helpers.ts`
- Test: `vera-frontend/src/pages/llmConfig.helpers.test.ts`

**Interfaces:**
- Consumes: `LlmConfigState` from Task 7.
- Produces: `SUGGESTED_MODELS: readonly string[]`, `hasPendingChange(input: string, current: LlmConfigState): boolean`, `canReset(current: LlmConfigState): boolean`, `formatUpdatedAt(iso: string | null): string` — consumed by Task 9's page.

- [ ] **Step 1: Write the failing test**

Create `vera-frontend/src/pages/llmConfig.helpers.test.ts`:

```ts
import { describe, expect, it } from "vitest"
import { canReset, formatUpdatedAt, hasPendingChange } from "@/pages/llmConfig.helpers"
import type { LlmConfigState } from "@/lib/api/llmConfig"

const state = (overrides: Partial<LlmConfigState> = {}): LlmConfigState => ({
  provider: "google",
  model: "gemini-2.5-flash",
  is_default: false,
  created_at: "2026-07-23T10:00:00Z",
  created_by_user_id: "u1",
  ...overrides,
})

describe("hasPendingChange", () => {
  it("false when input matches the saved override", () => {
    expect(hasPendingChange("gemini-2.5-flash", state())).toBe(false)
  })
  it("true when input differs", () => {
    expect(hasPendingChange("gemini-3.5-flash", state())).toBe(true)
  })
  it("compares against empty string when at default (model is null)", () => {
    expect(hasPendingChange("", state({ model: null, is_default: true }))).toBe(false)
    expect(hasPendingChange("gemini-2.5-flash", state({ model: null, is_default: true }))).toBe(
      true,
    )
  })
})

describe("canReset", () => {
  it("false at default", () => {
    expect(canReset(state({ model: null, is_default: true }))).toBe(false)
  })
  it("true when overridden", () => {
    expect(canReset(state())).toBe(true)
  })
})

describe("formatUpdatedAt", () => {
  it("dash for null", () => {
    expect(formatUpdatedAt(null)).toBe("—")
  })
  it("formats a valid ISO date", () => {
    expect(formatUpdatedAt("2026-07-23T10:00:00Z")).not.toBe("—")
  })
  it("dash for an unparseable string", () => {
    expect(formatUpdatedAt("not-a-date")).toBe("—")
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd vera-frontend && npx vitest run src/pages/llmConfig.helpers.test.ts`
Expected: FAIL (`Cannot find module '@/pages/llmConfig.helpers'`).

- [ ] **Step 3: Write the helpers**

Create `vera-frontend/src/pages/llmConfig.helpers.ts`:

```ts
import type { LlmConfigState } from "@/lib/api/llmConfig"

export const SUGGESTED_MODELS = [
  "gemini-2.5-flash",
  "gemini-3.1-flash-lite",
  "gemini-3.5-flash",
  "gemini-3.6-flash",
] as const

/** Whether the input differs from the currently saved effective value — gates the
 *  Save button so a no-op save isn't offered. A default (model: null) reads as "". */
export function hasPendingChange(input: string, current: LlmConfigState): boolean {
  return input.trim() !== (current.model ?? "")
}

/** Reset only makes sense when an override is actually active. */
export function canReset(current: LlmConfigState): boolean {
  return !current.is_default
}

export function formatUpdatedAt(iso: string | null): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd vera-frontend && npx vitest run src/pages/llmConfig.helpers.test.ts`
Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/pages/llmConfig.helpers.ts vera-frontend/src/pages/llmConfig.helpers.test.ts
git commit -m "feat: add llmConfig page helpers"
```

---

### Task 9: Frontend page + nav entry + route

**Files:**
- Create: `vera-frontend/src/pages/LlmConfig.tsx`
- Modify: `vera-frontend/src/lib/nav.ts`
- Modify: `vera-frontend/src/App.tsx`

**Interfaces:**
- Consumes: `getLlmConfig`, `getLlmConfigHistory`, `saveLlmConfig`, `resetLlmConfig`, `LlmConfigState` (Task 7); `SUGGESTED_MODELS`, `hasPendingChange`, `canReset`, `formatUpdatedAt` (Task 8); `selectIsSuperAdmin` from `@/store/authSlice` (existing); `ApiError` from `@/lib/api/client` (existing).
- Produces: route `/voice-model`, nav entry gated on `platform:llm_config:read`.

- [ ] **Step 1: Write the page component**

Create `vera-frontend/src/pages/LlmConfig.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { ApiError } from "@/lib/api/client"
import {
  getLlmConfig,
  getLlmConfigHistory,
  resetLlmConfig,
  saveLlmConfig,
  type LlmConfigState,
} from "@/lib/api/llmConfig"
import {
  SUGGESTED_MODELS, canReset, formatUpdatedAt, hasPendingChange,
} from "@/pages/llmConfig.helpers"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

export function LlmConfig() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [current, setCurrent] = useState<LlmConfigState | null>(null)
  const [history, setHistory] = useState<LlmConfigState[] | null>(null)
  const [input, setInput] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Refresh after a mutation.
  const load = useCallback(async () => {
    setError(null)
    try {
      const [cfg, hist] = await Promise.all([getLlmConfig(), getLlmConfigHistory()])
      setCurrent(cfg)
      setInput(cfg.model ?? "")
      setHistory(hist)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the LLM model config.")
      setHistory((prev) => prev ?? [])
    }
  }, [])

  // Initial load — cancelled-flag idiom, matching InsuranceProviders.tsx / IvrPlaybooks.tsx.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    Promise.all([getLlmConfig(), getLlmConfigHistory()])
      .then(([cfg, hist]) => {
        if (cancelled) return
        setCurrent(cfg)
        setInput(cfg.model ?? "")
        setHistory(hist)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : "Could not load the LLM model config.")
        setHistory((prev) => prev ?? [])
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  async function onSave() {
    setError(null)
    setBusy(true)
    try {
      await saveLlmConfig(input)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the model override.")
    } finally {
      setBusy(false)
    }
  }

  async function onReset() {
    setError(null)
    setBusy(true)
    try {
      await resetLlmConfig()
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reset the model override.")
    } finally {
      setBusy(false)
    }
  }

  if (!isSuperAdmin) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Voice Model</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          This setting is managed by platform operators only.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Voice Model</h1>
        <p className="text-sm text-muted-foreground">
          Overrides the Gemini model the voice cascade's LLM stage uses, platform-wide.
          Applies to calls dispatched after saving — in-flight calls are unaffected.
        </p>
      </div>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      {current && (
        <div className="space-y-4 rounded-lg border border-border p-4">
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">Current:</span>
            <Badge variant={current.is_default ? "outline" : "default"}>
              {current.is_default ? "Default" : "Override"}
            </Badge>
            <span className="font-mono text-sm">
              {current.model ?? "gemini-2.5-flash (hardcoded default)"}
            </span>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="llm-model-input">Model name</Label>
            <Input
              id="llm-model-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="e.g. gemini-3.5-flash"
            />
          </div>

          <div className="flex flex-wrap gap-2">
            {SUGGESTED_MODELS.map((m) => (
              <Button
                key={m}
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setInput(m)}
              >
                {m}
              </Button>
            ))}
          </div>

          <div className="flex gap-3">
            <Button
              onClick={onSave}
              disabled={busy || !hasPendingChange(input, current)}
              className="min-w-[100px]"
            >
              {busy ? "Saving…" : "Save"}
            </Button>
            <Button variant="outline" onClick={onReset} disabled={busy || !canReset(current)}>
              Reset to default
            </Button>
          </div>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-sm font-semibold text-muted-foreground">History</h2>
        <div className="rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Changed</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {history === null && (
                <TableRow>
                  <TableCell colSpan={2} className="py-6 text-center text-muted-foreground">
                    Loading…
                  </TableCell>
                </TableRow>
              )}
              {history?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={2} className="py-6 text-center text-muted-foreground">
                    No changes yet.
                  </TableCell>
                </TableRow>
              )}
              {history?.map((row, i) => (
                <TableRow key={`${row.created_at}-${i}`}>
                  <TableCell className="font-mono text-sm">
                    {row.model ?? "Reset to default"}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {formatUpdatedAt(row.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Add the nav entry**

In `vera-frontend/src/lib/nav.ts`, add `Cpu` to the `lucide-react` import:

```ts
import {
  Activity,
  Bot,
  Building2,
  Cpu,
  PhoneCall,
  BarChart3,
  Database,
  FileText,
  KeyRound,
  ListTree,
  Mic,
  Settings,
  Users,
  type LucideIcon,
} from "lucide-react"
```

And add a `NavItem` entry to `navItems`, after the `"Form Schemas"` entry:

```ts
  { title: "Form Schemas", to: "/form-schemas", icon: FileText, permission: "platform:form_schemas:read" },
  { title: "Voice Model", to: "/voice-model", icon: Cpu, permission: "platform:llm_config:read" },
```

- [ ] **Step 3: Register the route**

In `vera-frontend/src/App.tsx`, add the import:

```tsx
import { LlmConfig } from "@/pages/LlmConfig"
```

And add the route after the `form-schemas` route (same bare, unwrapped convention as the other super-admin-only pages — no `RequireNavRoute`):

```tsx
            {/* Super-admin-only voice cascade LLM model override. */}
            <Route path="voice-model" element={<LlmConfig />} />
```

- [ ] **Step 4: Typecheck, lint, test, build**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all PASS with no new errors.

- [ ] **Step 5: Manual verification**

With `vera-backend` running (`just up`, `just migrate`, `just api`) and `vera-frontend`'s dev server running (`npm run dev`):
1. Log in as a platform SUPER_ADMIN.
2. Navigate to `/voice-model`. Confirm it shows "Default" and `gemini-2.5-flash (hardcoded default)`.
3. Click the `gemini-3.5-flash` quick-pick, click Save. Confirm the badge switches to "Override" and the history list shows the new row.
4. Click "Reset to default". Confirm it reverts and a "Reset to default" row appears in history.
5. Log in as a tenant user (non-superadmin) and confirm `/voice-model` shows the "managed by platform operators only" message and the nav item is hidden.

- [ ] **Step 6: Commit**

```bash
git add vera-frontend/src/pages/LlmConfig.tsx vera-frontend/src/lib/nav.ts vera-frontend/src/App.tsx
git commit -m "feat: add Voice Model super-admin page"
```

---

### Task 10: Mandatory simplify pass + final full verification

Per the repo-root `CLAUDE.md` rule ("MANDATORY: simplify code after every implementation"), this must run in the same session as the implementation, over the whole diff from Tasks 1-9.

- [ ] **Step 1: Run the code-simplifier**

Invoke the `code-simplifier` agent (trigger phrase "simplify code") targeting the full set of files touched across Tasks 1-9 (list them explicitly if the agent needs a file list — every `Create`/`Modify` path listed across this plan's tasks).

- [ ] **Step 2: Re-run the backend gate**

Run: `cd vera-backend && just check`
Expected: PASS. If the simplifier changed anything, fix any resulting failures before proceeding.

- [ ] **Step 3: Re-run the frontend gate**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: PASS.

- [ ] **Step 4: Final commit (if the simplifier changed anything)**

```bash
git add -A
git commit -m "refactor: simplify voice model override implementation"
```

(Skip this commit if the simplifier made no changes.)
