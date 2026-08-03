# Per-Model Thinking Config (budget/level) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a platform superadmin tune the voice cascade LLM's "thinking" config (Gemini 2.x `thinking_budget` int, Gemini 3.x `thinking_level` enum) per saved model, fixing the current unconditional-`thinking_budget=0` warning/silent-no-op for Gemini 3 models, and trace the active model + thinking config on the per-call span.

**Architecture:** A new nullable `extra_config` JSONB column on the existing `voice_model_config` table holds the current thinking override (validated via a `ThinkingOverride` Pydantic model, family-checked against the model name so an incompatible pairing — which would crash the plugin — is rejected at save time, not dispatch time). The value rides the same dispatch-metadata channel the model override already uses, reaches `cascade.py` via a new `build_session` parameter, and is also used to set `vera.llm.*` span attributes on the per-call root span. Frontend gains a second, model-family-reactive control (budget number input vs. level dropdown) on the existing page.

**Tech Stack:** Same as the parent feature — Python/FastAPI/SQLAlchemy/Alembic/pytest (`vera-backend/`); React/TypeScript/Vitest (`vera-frontend/`).

## Global Constraints

- Family detection is a plain substring check, mirroring `livekit-plugins-google`'s own internal `_is_gemini_3_model`: `"gemini-3" in model.lower()`. Keep this exact heuristic in one place (`vera_core.services.model_config.is_gemini_3_model`) and import it everywhere else that needs it (never re-implement the substring check).
- `thinking_level` on a pre-Gemini-3 model makes the plugin **raise `ValueError`** (not just warn) — validation preventing this must happen at save time (422), not be deferred to dispatch/call time.
- `thinking_budget` is a free-form integer (no preset dropdown). `thinking_level` exposes the full enum: `minimal`, `low`, `medium`, `high`.
- Exactly one of `thinking_budget`/`thinking_level` may be set at a time — both, or neither (while the object itself is present), is invalid.
- No admin override → an explicit, deterministic default per family: `thinking_budget=0` for pre-3 (unchanged), `thinking_level="low"` for Gemini-3 (a new, explicit default — replacing reliance on the plugin's own private auto-selection).
- New span attributes use the existing `vera.*` prefix convention (`vera.room`, `vera.tenant_id`, `vera.call_id` in `call_trace_attributes`) — never `langfuse.*` (reserved for Langfuse-native keys) or the SDK-owned `gen_ai.*` keys.
- New migration must be idempotent (`ADD COLUMN IF NOT EXISTS`) — do not edit the already-applied Task-1 migration from the parent feature.
- Backend final gate: `just check` (from `vera-backend/`). Frontend final gate: `npx tsc -b && npx eslint . && npm test && npm run build` (from `vera-frontend/`).
- Per repo-root `CLAUDE.md`: run the code-simplifier over the full diff and re-verify both gates before considering the work done — this is the final task below.
- No `Co-Authored-By: Claude` in any commit message.
- This continues the same branch/PR (`feat/dynamic-voice-llm-model-change-in-runtime`, PR #127) — not a new branch.

---

### Task 1: `extra_config` column + migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/voice_model_config.py`
- Create: `migrations/versions/<generated>_add_extra_config_to_voice_model_config.py`
- Modify: `tests/integration/db/test_voice_model_config.py`

**Interfaces:**
- Produces: `VoiceModelConfig.extra_config: dict[str, Any] | None` (JSONB column) — consumed by Task 2's service functions.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/db/test_voice_model_config.py` (after the existing `test_newest_row_per_stage_is_the_current_value`):

```python
async def test_extra_config_column_stores_arbitrary_json(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        row = VoiceModelConfig(
            stage="llm",
            provider="google",
            model="gemini-3.5-flash",
            extra_config={"thinking_level": "low"},
        )
        s.add(row)
        await s.flush()
        row_id = row.id

    async with admin_sessionmaker() as s:
        fetched = (
            await s.execute(select(VoiceModelConfig).where(VoiceModelConfig.id == row_id))
        ).scalar_one()
        assert fetched.extra_config == {"thinking_level": "low"}


async def test_extra_config_defaults_to_null(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        row = VoiceModelConfig(stage="llm", provider="google", model="gemini-2.5-flash")
        s.add(row)
        await s.flush()
        row_id = row.id

    async with admin_sessionmaker() as s:
        fetched = (
            await s.execute(select(VoiceModelConfig).where(VoiceModelConfig.id == row_id))
        ).scalar_one()
        assert fetched.extra_config is None
```

(No new import needed — `select`, `VoiceModelConfig`, `admin_sessionmaker` are already imported in this file. The existing autouse `cleanup` fixture already deletes every `stage == "llm"` row, so it covers these new rows too.)

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/integration/db/test_voice_model_config.py -v`
Expected: FAIL (`TypeError: 'extra_config' is an invalid keyword argument for VoiceModelConfig`).

- [ ] **Step 3: Add the column to the model**

In `packages/vera_core/src/vera_core/models/voice_model_config.py`, change the imports from:

```python
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
```

to:

```python
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
```

And add the new column after `model`:

```python
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extra_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )
```

- [ ] **Step 4: Generate the migration scaffold**

Run: `just makemigration "add extra_config to voice_model_config"`

Keep the generated `revision`/`down_revision`/`branch_labels`/`depends_on`/`Create Date` values (this should chain onto the current head, `e3e633747040`). Replace the docstring and `upgrade()`/`downgrade()`:

```python
"""add extra_config to voice_model_config

Additive, nullable JSONB column — NULL means "no thinking override, use the
per-family default" (see agent_worker/cascade.py::resolve_thinking_attrs). No
CHECK constraint: the single write path (vera_core.services.model_config.save_llm_model)
validates its shape via ThinkingOverride before insert.
"""

from alembic import op


def upgrade() -> None:
    op.execute("ALTER TABLE voice_model_config ADD COLUMN IF NOT EXISTS extra_config JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE voice_model_config DROP COLUMN IF EXISTS extra_config")
```

- [ ] **Step 5: Apply the migration**

Run: `just migrate`
Expected: no errors.

- [ ] **Step 6: Run the test to verify it passes**

Run: `just test tests/integration/db/test_voice_model_config.py -v`
Expected: all 6 PASS (4 pre-existing + 2 new).

- [ ] **Step 7: Full backend gate + commit**

Run: `just check`

```bash
git add packages/vera_core/src/vera_core/models/voice_model_config.py \
        migrations/versions/ \
        tests/integration/db/test_voice_model_config.py
git commit -m "feat: add extra_config JSONB column to voice_model_config"
```

---

### Task 2: `ThinkingOverride` + family validation + dispatch metadata

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/model_config.py`
- Modify: `tests/unit/test_model_config.py`
- Modify: `tests/integration/db/test_model_config_service.py`
- Modify: `tests/integration/control_plane/test_llm_model_dispatch.py`

**Interfaces:**
- Consumes: `VoiceModelConfig.extra_config` from Task 1.
- Produces (from `vera_core.services.model_config`): `class ThinkingOverride(BaseModel)` (`thinking_budget: int | None`, `thinking_level: Literal["minimal","low","medium","high"] | None`, exactly-one validator), `def is_gemini_3_model(model: str) -> bool`, `class InvalidThinkingOverride(ValueError)`, `def validate_extra_config(model: str, extra_config: ThinkingOverride | None) -> None`. `save_llm_model` gains a required keyword `extra_config: ThinkingOverride | None`. `add_llm_model_override_metadata` also sets `metadata["llm_thinking_override"]` when present — consumed by Task 3 (endpoint) and Task 4 (cascade.py); `is_gemini_3_model` is also imported directly by Task 4.

- [ ] **Step 1: Write the failing unit tests**

Add to `tests/unit/test_model_config.py` (new imports at top, alongside the existing `from vera_core.services.model_config import InvalidModelName, normalize_model_name`):

```python
from vera_core.services.model_config import (
    InvalidModelName,
    InvalidThinkingOverride,
    ThinkingOverride,
    is_gemini_3_model,
    normalize_model_name,
    validate_extra_config,
)
```

Append:

```python
def test_is_gemini_3_model_matches_suggested_gemini_3_names() -> None:
    for model in ("gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "GEMINI-3-PRO"):
        assert is_gemini_3_model(model) is True


def test_is_gemini_3_model_false_for_pre_3() -> None:
    for model in ("gemini-2.5-flash", "gemini-1.5-pro"):
        assert is_gemini_3_model(model) is False


def test_thinking_override_rejects_both_fields_set() -> None:
    with pytest.raises(ValidationError):
        ThinkingOverride(thinking_budget=0, thinking_level="low")


def test_thinking_override_rejects_neither_field_set() -> None:
    with pytest.raises(ValidationError):
        ThinkingOverride()


def test_thinking_override_accepts_budget_only() -> None:
    assert ThinkingOverride(thinking_budget=500).thinking_budget == 500


def test_thinking_override_accepts_level_only() -> None:
    assert ThinkingOverride(thinking_level="high").thinking_level == "high"


def test_validate_extra_config_accepts_none() -> None:
    validate_extra_config("gemini-2.5-flash", None)  # no raise


def test_validate_extra_config_accepts_matching_pairs() -> None:
    validate_extra_config("gemini-2.5-flash", ThinkingOverride(thinking_budget=0))
    validate_extra_config("gemini-3.5-flash", ThinkingOverride(thinking_level="low"))


def test_validate_extra_config_rejects_level_on_pre_3_model() -> None:
    with pytest.raises(InvalidThinkingOverride):
        validate_extra_config("gemini-2.5-flash", ThinkingOverride(thinking_level="low"))


def test_validate_extra_config_rejects_budget_on_gemini_3_model() -> None:
    with pytest.raises(InvalidThinkingOverride):
        validate_extra_config("gemini-3.5-flash", ThinkingOverride(thinking_budget=0))
```

Also add `import pytest` and `from pydantic import ValidationError` to the top of the file if not already present (check the current file first — it currently only imports `pytest` and the `model_config` names).

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/unit/test_model_config.py -v`
Expected: FAIL (`ImportError` — none of the new names exist yet).

- [ ] **Step 3: Implement in `model_config.py`**

Change the imports at the top of `packages/vera_core/src/vera_core/services/model_config.py` from:

```python
import logging
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import VoiceModelConfig
from vera_core.models.enums import VoiceModelStage
```

to:

```python
import logging
import re
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import VoiceModelConfig
from vera_core.models.enums import VoiceModelStage
```

Add after `class InvalidModelName(ValueError): pass` and before `def normalize_model_name`:

```python
class ThinkingOverride(BaseModel):
    thinking_budget: int | None = None
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ThinkingOverride":
        if (self.thinking_budget is None) == (self.thinking_level is None):
            raise ValueError("exactly one of thinking_budget or thinking_level must be set")
        return self


def is_gemini_3_model(model: str) -> bool:
    """Mirrors livekit-plugins-google's own detection (llm.py::_is_gemini_3_model) —
    keep this in lockstep with that heuristic; drift here would let an incompatible
    thinking_level/thinking_budget pairing reach the plugin, which raises ValueError
    for thinking_level on a pre-3 model."""
    return "gemini-3" in model.lower()


class InvalidThinkingOverride(ValueError):
    pass


def validate_extra_config(model: str, extra_config: ThinkingOverride | None) -> None:
    if extra_config is None:
        return
    is_gemini_3 = is_gemini_3_model(model)
    if extra_config.thinking_level is not None and not is_gemini_3:
        raise InvalidThinkingOverride(
            f"thinking_level requires a Gemini 3 model; {model!r} is not Gemini 3"
        )
    if extra_config.thinking_budget is not None and is_gemini_3:
        raise InvalidThinkingOverride(
            f"thinking_budget is not supported on Gemini 3 models ({model!r}) — "
            "use thinking_level instead"
        )
```

Replace `save_llm_model`:

```python
async def save_llm_model(
    session: AsyncSession,
    raw_model: str,
    *,
    extra_config: ThinkingOverride | None,
    created_by_user_id: UUID | None,
) -> VoiceModelConfig:
    model = normalize_model_name(raw_model)
    validate_extra_config(model, extra_config)
    row = VoiceModelConfig(
        stage=VoiceModelStage.LLM,
        provider=_LLM_PROVIDER,
        model=model,
        extra_config=extra_config.model_dump(exclude_none=True) if extra_config else None,
        created_by_user_id=created_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row
```

Replace the body of `add_llm_model_override_metadata`'s final block:

```python
    if current is not None:
        metadata["llm_model_override"] = current.model
        if current.extra_config:
            metadata["llm_thinking_override"] = current.extra_config
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `just test tests/unit/test_model_config.py -v`
Expected: all 17 PASS (7 pre-existing + 10 new).

- [ ] **Step 5: Update the existing integration tests' `save_llm_model` call sites**

`save_llm_model` now requires `extra_config` as a keyword argument. In `tests/integration/db/test_model_config_service.py`, update every existing call:

- `test_save_then_get_active_returns_the_saved_row`: change
  `await save_llm_model(s, " gemini-3.5-flash ", created_by_user_id=None)` to
  `await save_llm_model(s, " gemini-3.5-flash ", extra_config=None, created_by_user_id=None)`.
- `test_reset_after_save_clears_and_history_shows_it`: change
  `await save_llm_model(s, "gemini-3.5-flash", created_by_user_id=None)` to
  `await save_llm_model(s, "gemini-3.5-flash", extra_config=None, created_by_user_id=None)`.
- `test_add_llm_model_override_metadata_sets_key_when_active`: change
  `await save_llm_model(s, "gemini-3.6-flash", created_by_user_id=None)` to
  `await save_llm_model(s, "gemini-3.6-flash", extra_config=None, created_by_user_id=None)`.

- [ ] **Step 6: Run it to verify these still pass**

Run: `just test tests/integration/db/test_model_config_service.py -v`
Expected: all 7 pre-existing tests still PASS (signature updated, behavior unchanged).

- [ ] **Step 7: Write new integration tests for the override + validation + metadata threading**

Add to `tests/integration/db/test_model_config_service.py` (add `ThinkingOverride, InvalidThinkingOverride` to the existing `from vera_core.services.model_config import (...)` block):

```python
async def test_save_with_matching_thinking_override_succeeds(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        saved = await save_llm_model(
            s,
            "gemini-3.5-flash",
            extra_config=ThinkingOverride(thinking_level="high"),
            created_by_user_id=None,
        )
        assert saved.extra_config == {"thinking_level": "high"}


async def test_save_rejects_mismatched_thinking_override(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        with pytest.raises(InvalidThinkingOverride):
            await save_llm_model(
                s,
                "gemini-3.5-flash",
                extra_config=ThinkingOverride(thinking_budget=0),
                created_by_user_id=None,
            )


async def test_add_llm_model_override_metadata_threads_thinking_override(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        await save_llm_model(
            s,
            "gemini-3.6-flash",
            extra_config=ThinkingOverride(thinking_level="minimal"),
            created_by_user_id=None,
        )

    async with admin_sessionmaker() as s:
        metadata: dict[str, object] = {}
        await add_llm_model_override_metadata(s, metadata)
        assert metadata == {
            "llm_model_override": "gemini-3.6-flash",
            "llm_thinking_override": {"thinking_level": "minimal"},
        }


async def test_add_llm_model_override_metadata_omits_thinking_key_when_not_set(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        await save_llm_model(s, "gemini-2.5-flash", extra_config=None, created_by_user_id=None)

    async with admin_sessionmaker() as s:
        metadata: dict[str, object] = {}
        await add_llm_model_override_metadata(s, metadata)
        assert metadata == {"llm_model_override": "gemini-2.5-flash"}
        assert "llm_thinking_override" not in metadata
```

Add `import pytest` at the top if not already present (check first — the file may already import it for other reasons; it currently does not, so add it).

- [ ] **Step 8: Run it to verify these pass**

Run: `just test tests/integration/db/test_model_config_service.py -v`
Expected: all 11 PASS (7 pre-existing + 4 new).

- [ ] **Step 9: Write the failing end-to-end dispatch test**

Add to `tests/integration/control_plane/test_llm_model_dispatch.py` (add `ThinkingOverride`'s dict shape doesn't need a new import — `VoiceModelConfig`/`VoiceModelStage` are already imported):

```python
async def test_voice_lab_carries_thinking_override_into_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(
            VoiceModelConfig(
                stage=VoiceModelStage.LLM,
                provider="google",
                model="gemini-3.5-flash",
                extra_config={"thinking_level": "high"},
            )
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
        assert meta["llm_thinking_override"] == {"thinking_level": "high"}
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))
```

Add `from sqlalchemy import delete` to the imports if not already present (the file already imports `delete` from `sqlalchemy` for the other test's cleanup — reuse it, no new import needed).

- [ ] **Step 10: Run it to verify it passes**

Run: `just test tests/integration/control_plane/test_llm_model_dispatch.py -v`
Expected: all 3 PASS (2 pre-existing + 1 new) — no production code changes needed here: `queue_dispatcher.py`/`voice_lab.py` already call `add_llm_model_override_metadata` unconditionally, so Step 3's change to that function is all that's needed for this to pass.

- [ ] **Step 11: Full backend gate + commit**

Run: `just check`

```bash
git add packages/vera_core/src/vera_core/services/model_config.py \
        tests/unit/test_model_config.py \
        tests/integration/db/test_model_config_service.py \
        tests/integration/control_plane/test_llm_model_dispatch.py
git commit -m "feat: add ThinkingOverride validation and thread it into dispatch metadata"
```

---

### Task 3: control_plane endpoint updates

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/llm_config.py`
- Modify: `tests/integration/control_plane/test_llm_config.py`

**Interfaces:**
- Consumes: `ThinkingOverride`, `InvalidThinkingOverride`, `save_llm_model`'s new signature from Task 2.
- Produces: `SaveLlmConfigRequest` and `LlmConfigState` both gain `extra_config: ThinkingOverride | None = None` — this is the exact shape Task 5 (frontend) types against.

- [ ] **Step 1: Update the router**

In `apps/control_plane/src/control_plane/api/v1/llm_config.py`, change the import block from:

```python
from vera_core.services.model_config import (
    InvalidModelName,
    get_active_llm_config,
    list_llm_config_history,
    reset_llm_model,
    save_llm_model,
)
```

to:

```python
from vera_core.services.model_config import (
    InvalidModelName,
    InvalidThinkingOverride,
    ThinkingOverride,
    get_active_llm_config,
    list_llm_config_history,
    reset_llm_model,
    save_llm_model,
)
```

Change `SaveLlmConfigRequest`:

```python
class SaveLlmConfigRequest(BaseModel):
    model: str
    extra_config: ThinkingOverride | None = None
```

Change `LlmConfigState`. **Important: its `extra_config` is a plain `dict`, not
`ThinkingOverride`** — `ThinkingOverride` stays the *request-only* validation type.
If the response field were typed `ThinkingOverride | None`, FastAPI would serialize
*every* field the Pydantic model declares, including the unset one as an explicit
`null` (e.g. `{"thinking_budget": null, "thinking_level": "high"}`) — breaking the
frontend's discriminated-union `"thinking_budget" in extra_config` checks, which
assume the unset key is genuinely *absent*, not present-and-null. `row.extra_config`
is already the correctly-trimmed dict (`model_dump(exclude_none=True)` at save
time), so return it as-is:

```python
class LlmConfigState(BaseModel):
    provider: str | None
    model: str | None
    extra_config: dict[str, int | str] | None
    is_default: bool
    created_at: datetime | None
    created_by_user_id: UUID | None
```

Change `_state`:

```python
def _state(row: VoiceModelConfig | None) -> LlmConfigState:
    if row is None:
        return LlmConfigState(
            provider=None,
            model=None,
            extra_config=None,
            is_default=True,
            created_at=None,
            created_by_user_id=None,
        )
    return LlmConfigState(
        provider=row.provider,
        model=row.model,
        extra_config=row.extra_config,
        is_default=row.model is None,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )
```

Change `save_llm_config`'s body:

```python
    try:
        row = await save_llm_model(
            session, body.model, extra_config=body.extra_config, created_by_user_id=caller.user_id
        )
    except InvalidModelName as exc:
        raise CustomAPIException(DefaultExceptionCode.VALIDATION_ERROR, message=str(exc)) from exc
    except InvalidThinkingOverride as exc:
        raise CustomAPIException(DefaultExceptionCode.VALIDATION_ERROR, message=str(exc)) from exc
    return ok(_state(row))
```

- [ ] **Step 2: Write the failing HTTP tests**

Add to `tests/integration/control_plane/test_llm_config.py`:

```python
async def test_save_with_matching_thinking_override_succeeds(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash", "extra_config": {"thinking_level": "high"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["extra_config"] == {"thinking_level": "high"}


async def test_save_rejects_mismatched_thinking_override(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash", "extra_config": {"thinking_budget": 0}},
    )
    assert resp.status_code == 422, resp.text


async def test_save_rejects_both_fields_set(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={
            "model": "gemini-2.5-flash",
            "extra_config": {"thinking_budget": 0, "thinking_level": "low"},
        },
    )
    assert resp.status_code == 422, resp.text


async def test_history_carries_extra_config(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-2.5-flash", "extra_config": {"thinking_budget": 500}},
    )
    history = await client.get("/api/v1/platform/llm-config/history", headers=_auth(w.super_token))
    assert history.status_code == 200, history.text
    assert history.json()["data"][0]["extra_config"] == {"thinking_budget": 500}
```

- [ ] **Step 3: Run it to verify it fails, then implement, then verify it passes**

Run: `just test tests/integration/control_plane/test_llm_config.py -v`
Expected: FAIL first (422 tests get 200s / body shape mismatches) since Step 1 hasn't been applied yet if run before Step 1 — implement Step 1 first, then run this and expect all 11 PASS (7 pre-existing + 4 new).

- [ ] **Step 4: Full backend gate + commit**

Run: `just check`

```bash
git add apps/control_plane/src/control_plane/api/v1/llm_config.py \
        tests/integration/control_plane/test_llm_config.py
git commit -m "feat: accept and validate extra_config on the platform llm-config endpoints"
```

---

### Task 4: agent_worker — `cascade.py` + `main.py` (thinking resolution + tracing)

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/cascade.py`
- Modify: `apps/agent_worker/src/agent_worker/main.py`
- Modify: `apps/agent_worker/tests/unit/test_cascade.py`

**Interfaces:**
- Consumes: `is_gemini_3_model` from `vera_core.services.model_config` (Task 2). Dispatch metadata key `"llm_thinking_override"` (a `dict[str, int|str] | None`, set by Task 2's `add_llm_model_override_metadata`).
- Produces: `resolve_thinking_attrs(model, thinking_override) -> dict[str, int | str]`, `resolve_thinking_config(model, thinking_override) -> ThinkingConfig`, `llm_trace_attributes(model, thinking_attrs) -> dict[str, str | int]` in `cascade.py`. `build_session(..., thinking_override: dict[str, Any] | None = None)`.

- [ ] **Step 1: Write the failing tests**

In `apps/agent_worker/tests/unit/test_cascade.py`, change the import line from:

```python
from agent_worker.cascade import cascade_session_kwargs, resolve_llm_model
```

to:

```python
from agent_worker.cascade import (
    cascade_session_kwargs,
    llm_trace_attributes,
    resolve_llm_model,
    resolve_thinking_attrs,
    resolve_thinking_config,
)
```

Append:

```python
def test_resolve_thinking_attrs_returns_explicit_override_verbatim() -> None:
    assert resolve_thinking_attrs("gemini-2.5-flash", {"thinking_budget": 500}) == {
        "thinking_budget": 500
    }
    assert resolve_thinking_attrs("gemini-3.5-flash", {"thinking_level": "high"}) == {
        "thinking_level": "high"
    }


def test_resolve_thinking_attrs_default_for_gemini_3_without_override() -> None:
    assert resolve_thinking_attrs("gemini-3.5-flash", None) == {"thinking_level": "low"}


def test_resolve_thinking_attrs_default_for_pre_3_without_override() -> None:
    assert resolve_thinking_attrs("gemini-2.5-flash", None) == {"thinking_budget": 0}


def test_resolve_thinking_config_builds_a_real_thinking_config_object() -> None:
    cfg = resolve_thinking_config("gemini-3.5-flash", {"thinking_level": "high"})
    assert cfg.thinking_budget is None
    assert cfg.thinking_level is not None

    cfg2 = resolve_thinking_config("gemini-2.5-flash", None)
    assert cfg2.thinking_budget == 0
    assert cfg2.thinking_level is None


def test_llm_trace_attributes_prefixes_vera_llm() -> None:
    attrs = llm_trace_attributes("gemini-3.5-flash", {"thinking_level": "low"})
    assert attrs == {"vera.llm.model": "gemini-3.5-flash", "vera.llm.thinking_level": "low"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/agent_worker && uv run pytest tests/unit/test_cascade.py -v`
Expected: FAIL (`ImportError` — the new names don't exist yet).

- [ ] **Step 3: Implement in `cascade.py`**

Change the import block from:

```python
from typing import Any

from google.genai.types import ThinkingConfig
from livekit.agents import AgentSession
from livekit.plugins import cartesia, deepgram, google, silero
from livekit.plugins.turn_detector.english import EnglishModel

from agent_worker.intervention import TakeoverState
```

to:

```python
from typing import Any

from google.genai.types import ThinkingConfig
from livekit.agents import AgentSession
from livekit.plugins import cartesia, deepgram, google, silero
from livekit.plugins.turn_detector.english import EnglishModel

from agent_worker.intervention import TakeoverState
from vera_core.services.model_config import is_gemini_3_model
```

Add after `resolve_llm_model` and before `cascade_session_kwargs`:

```python
def resolve_thinking_attrs(model: str, thinking_override: dict[str, Any] | None) -> dict[str, int | str]:
    """The resolved (budget-or-level) values in plain-value form — exactly one key,
    matching the same pairing ThinkingOverride/validate_extra_config enforce at save
    time. No override + Gemini 3 -> an explicit "low" (not an empty ThinkingConfig
    left for the plugin's own private auto-selection) so this is always accurate."""
    if thinking_override:
        return dict(thinking_override)
    if is_gemini_3_model(model):
        return {"thinking_level": "low"}
    return {"thinking_budget": 0}


def resolve_thinking_config(model: str, thinking_override: dict[str, Any] | None) -> ThinkingConfig:
    return ThinkingConfig(**resolve_thinking_attrs(model, thinking_override))


def llm_trace_attributes(model: str, thinking_attrs: dict[str, int | str]) -> dict[str, str | int]:
    return {"vera.llm.model": model, **{f"vera.llm.{k}": v for k, v in thinking_attrs.items()}}
```

Update `build_session`'s signature and body:

```python
def build_session(
    vad: Any | None = None,
    *,
    key_terms: list[str] | None = None,
    llm_model: str | None = None,
    thinking_override: dict[str, Any] | None = None,
) -> AgentSession[TakeoverState]:
    model = resolve_llm_model(llm_model)
    # The latch must exist from construction: agents read it before speaking or hanging up.
    return AgentSession(
        userdata=TakeoverState(),
        stt=deepgram.STTv2(
            model="flux-general-en", eager_eot_threshold=0.5, **stt_kwargs(key_terms)
        ),
        llm=google.LLM(
            model=model,
            vertexai=True,
            location="global",
            thinking_config=resolve_thinking_config(model, thinking_override),
        ),
        tts=cartesia.TTS(model="sonic-3.5", emotion=["confident"]),
        vad=vad if vad is not None else _build_vad(),
        **cascade_session_kwargs(turn_detector=EnglishModel()),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/agent_worker && uv run pytest tests/unit/test_cascade.py -v`
Expected: all 14 PASS (9 pre-existing + 5 new).

- [ ] **Step 5: Wire `main.py`**

Change the import line:

```python
from agent_worker.cascade import _build_vad, build_session
```

to:

```python
from agent_worker.cascade import (
    _build_vad,
    build_session,
    llm_trace_attributes,
    resolve_llm_model,
    resolve_thinking_attrs,
)
```

Change the `build_session(...)` call site in `entrypoint()` (currently):

```python
        session = build_session(
            vad=ctx.proc.userdata.get("vad"),
            key_terms=controller.plan.stt_key_terms if controller is not None else None,
            llm_model=meta.get("llm_model_override"),
        )
```

to:

```python
        resolved_model = resolve_llm_model(meta.get("llm_model_override"))
        thinking_attrs = resolve_thinking_attrs(resolved_model, meta.get("llm_thinking_override"))
        trace.get_current_span().set_attributes(llm_trace_attributes(resolved_model, thinking_attrs))

        session = build_session(
            vad=ctx.proc.userdata.get("vad"),
            key_terms=controller.plan.stt_key_terms if controller is not None else None,
            llm_model=meta.get("llm_model_override"),
            thinking_override=meta.get("llm_thinking_override"),
        )
```

(`trace` is already imported at the top of `main.py` — `from opentelemetry import trace` — used a few lines earlier at `trace.get_current_span().set_attributes(call_trace_attributes(room_name))`; no new import needed for that name.)

- [ ] **Step 6: Run the full agent_worker test suite**

Run: `cd apps/agent_worker && uv run pytest tests/ -v`
Expected: all PASS, no regressions.

- [ ] **Step 7: Full backend gate + commit**

Run: `cd vera-backend && just check`

```bash
git add apps/agent_worker/src/agent_worker/cascade.py \
        apps/agent_worker/src/agent_worker/main.py \
        apps/agent_worker/tests/unit/test_cascade.py
git commit -m "feat: resolve per-model thinking config and trace it on the call span"
```

---

### Task 5: Frontend API layer — `llmConfig.ts`

**Files:**
- Modify: `vera-frontend/src/lib/api/llmConfig.ts`
- Modify: `vera-frontend/src/lib/api/llmConfig.test.ts`

**Interfaces:**
- Produces: `type ThinkingOverride = { thinking_budget: number; thinking_level?: never } | { thinking_level: "minimal"|"low"|"medium"|"high"; thinking_budget?: never }`. `LlmConfigState` gains `extra_config: ThinkingOverride | null`. `saveLlmConfig(model: string, extraConfig?: ThinkingOverride | null)` — consumed by Task 6 (helpers) and Task 7 (page).

- [ ] **Step 1: Write the failing test**

First, check the current content of `vera-frontend/src/lib/api/llmConfig.test.ts` (it exists from the parent feature — 4 tests) and update its `saveLlmConfig` test to also assert the new `extra_config` field in the body, and add one more test for the two-argument call. Replace the existing test:

```ts
  it("PUTs the model with the conventional Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await saveLlmConfig("gemini-3.5-flash")
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config", {
      method: "PUT",
      body: { model: "gemini-3.5-flash" },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
```

with:

```ts
  it("PUTs the model with no extra_config when omitted", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await saveLlmConfig("gemini-3.5-flash")
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config", {
      method: "PUT",
      body: { model: "gemini-3.5-flash", extra_config: null },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("PUTs the model with a thinking override when provided", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await saveLlmConfig("gemini-3.5-flash", { thinking_level: "high" })
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config", {
      method: "PUT",
      body: { model: "gemini-3.5-flash", extra_config: { thinking_level: "high" } },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd vera-frontend && npx vitest run src/lib/api/llmConfig.test.ts`
Expected: FAIL (`saveLlmConfig` doesn't yet send `extra_config`).

- [ ] **Step 3: Update `llmConfig.ts`**

Replace the file's `LlmConfigState` type and `saveLlmConfig` function:

```ts
// Platform (super admin) voice-cascade LLM model override endpoints.
// Mirrors backend api/v1/llm_config.py.
import { apiRequest, randomId } from "@/lib/api/client"

export type ThinkingOverride =
  | { thinking_budget: number; thinking_level?: never }
  | { thinking_level: "minimal" | "low" | "medium" | "high"; thinking_budget?: never }

export type LlmConfigState = {
  provider: string | null
  model: string | null
  extra_config: ThinkingOverride | null
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

export function saveLlmConfig(model: string, extraConfig: ThinkingOverride | null = null) {
  return apiRequest<LlmConfigState>("/platform/llm-config", {
    method: "PUT",
    body: { model, extra_config: extraConfig },
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
Expected: all 5 PASS (the 3 unchanged pre-existing + 2 replacing/new).

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/api/llmConfig.ts vera-frontend/src/lib/api/llmConfig.test.ts
git commit -m "feat: add ThinkingOverride to the llmConfig API client"
```

---

### Task 6: Frontend helpers — `llmConfig.helpers.ts`

**Files:**
- Modify: `vera-frontend/src/pages/llmConfig.helpers.ts`
- Modify: `vera-frontend/src/pages/llmConfig.helpers.test.ts`

**Interfaces:**
- Consumes: `LlmConfigState`, `ThinkingOverride` from Task 5.
- Produces: `isGemini3Model(model: string): boolean`, `THINKING_LEVELS: readonly ["minimal","low","medium","high"]`. `hasPendingChange` gains a second parameter for the extra-config comparison — consumed by Task 7's page.

- [ ] **Step 1: Write the failing tests**

Replace `vera-frontend/src/pages/llmConfig.helpers.test.ts`'s `hasPendingChange` describe block and add new ones. The file's current `state` fixture and other describe blocks (`canReset`, `formatUpdatedAt`) stay as-is; only change `hasPendingChange`'s tests and add two new describe blocks:

```ts
describe("hasPendingChange", () => {
  it("false when input matches the saved override and extra_config is unchanged", () => {
    expect(hasPendingChange("gemini-2.5-flash", null, state())).toBe(false)
  })
  it("true when model input differs", () => {
    expect(hasPendingChange("gemini-3.5-flash", null, state())).toBe(true)
  })
  it("compares against empty string when at default (model is null)", () => {
    expect(hasPendingChange("", null, state({ model: null, is_default: true }))).toBe(false)
    expect(
      hasPendingChange("gemini-2.5-flash", null, state({ model: null, is_default: true })),
    ).toBe(true)
  })
  it("true when extra_config differs even if model is unchanged", () => {
    expect(
      hasPendingChange("gemini-2.5-flash", { thinking_budget: 500 }, state()),
    ).toBe(true)
  })
  it("false when extra_config matches the saved value", () => {
    const saved = state({ extra_config: { thinking_budget: 0 } })
    expect(hasPendingChange("gemini-2.5-flash", { thinking_budget: 0 }, saved)).toBe(false)
  })
})

describe("isGemini3Model", () => {
  it("true for suggested Gemini 3 names", () => {
    expect(isGemini3Model("gemini-3.1-flash-lite")).toBe(true)
    expect(isGemini3Model("gemini-3.5-flash")).toBe(true)
    expect(isGemini3Model("GEMINI-3-PRO")).toBe(true)
  })
  it("false for pre-3 names", () => {
    expect(isGemini3Model("gemini-2.5-flash")).toBe(false)
  })
})

describe("THINKING_LEVELS", () => {
  it("is the full four-value enum, in order", () => {
    expect(THINKING_LEVELS).toEqual(["minimal", "low", "medium", "high"])
  })
})
```

Update the file's imports and the `state` fixture default at the top:

```ts
import { describe, expect, it } from "vitest"
import { THINKING_LEVELS, canReset, formatUpdatedAt, hasPendingChange, isGemini3Model } from "@/pages/llmConfig.helpers"
import type { LlmConfigState } from "@/lib/api/llmConfig"

const state = (overrides: Partial<LlmConfigState> = {}): LlmConfigState => ({
  provider: "google",
  model: "gemini-2.5-flash",
  extra_config: null,
  is_default: false,
  created_at: "2026-07-23T10:00:00Z",
  created_by_user_id: "u1",
  ...overrides,
})
```

(The rest of the file — `canReset` and `formatUpdatedAt` describe blocks — stay exactly as they are; only the import line, the `state` fixture, and `hasPendingChange`'s tests change, plus the two new describe blocks appended.)

- [ ] **Step 2: Run it to verify it fails**

Run: `cd vera-frontend && npx vitest run src/pages/llmConfig.helpers.test.ts`
Expected: FAIL (`isGemini3Model`/`THINKING_LEVELS` don't exist; `hasPendingChange` called with the wrong arity).

- [ ] **Step 3: Update `llmConfig.helpers.ts`**

Replace the whole file:

```ts
import type { LlmConfigState, ThinkingOverride } from "@/lib/api/llmConfig"

export const SUGGESTED_MODELS = [
  "gemini-2.5-flash",
  "gemini-3.1-flash-lite",
  "gemini-3.5-flash",
  "gemini-3.6-flash",
] as const

export const THINKING_LEVELS = ["minimal", "low", "medium", "high"] as const

/** Mirrors the backend's vera_core.services.model_config.is_gemini_3_model exactly —
 *  keep both in lockstep; a drifted heuristic would let the wrong thinking control
 *  render for a model name the backend would reject. */
export function isGemini3Model(model: string): boolean {
  return model.toLowerCase().includes("gemini-3")
}

/** Whether either the model input or the thinking override differs from what's
 *  currently saved — gates the Save button so a no-op save isn't offered. A
 *  default (model: null) reads as "". */
export function hasPendingChange(
  input: string,
  extraConfig: ThinkingOverride | null,
  current: LlmConfigState,
): boolean {
  if (input.trim() !== (current.model ?? "")) return true
  return JSON.stringify(extraConfig) !== JSON.stringify(current.extra_config)
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd vera-frontend && npx vitest run src/pages/llmConfig.helpers.test.ts`
Expected: all 13 PASS (`hasPendingChange`: 5, `canReset`: 2 pre-existing unchanged, `formatUpdatedAt`: 3 pre-existing unchanged, `isGemini3Model`: 2 new, `THINKING_LEVELS`: 1 new).

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/pages/llmConfig.helpers.ts vera-frontend/src/pages/llmConfig.helpers.test.ts
git commit -m "feat: add isGemini3Model and extra_config-aware hasPendingChange"
```

---

### Task 7: Frontend page — conditional thinking control

**Files:**
- Modify: `vera-frontend/src/pages/LlmConfig.tsx`

**Interfaces:**
- Consumes: `saveLlmConfig(model, extraConfig)`, `LlmConfigState.extra_config`, `ThinkingOverride` (Task 5); `isGemini3Model`, `THINKING_LEVELS`, updated `hasPendingChange` (Task 6).

- [ ] **Step 1: Update the page**

Add `Select` to the shadcn imports (used elsewhere in this codebase as a native-`<option>`-based wrapper — see `src/components/insurance-providers/ProviderFormDialog.tsx`) and `type ThinkingOverride` to the `llmConfig` API import; add `isGemini3Model`/`THINKING_LEVELS` to the helpers import:

```tsx
import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
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
  type ThinkingOverride,
} from "@/lib/api/llmConfig"
import {
  SUGGESTED_MODELS,
  THINKING_LEVELS,
  canReset,
  formatUpdatedAt,
  hasPendingChange,
  isGemini3Model,
} from "@/pages/llmConfig.helpers"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"
```

Add two more local states, populate them on load (in both `load()` and the mount effect), and compute the current `extraConfig` + `showLevel` flag:

```tsx
export function LlmConfig() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [current, setCurrent] = useState<LlmConfigState | null>(null)
  const [history, setHistory] = useState<LlmConfigState[] | null>(null)
  const [input, setInput] = useState("")
  const [thinkingBudgetInput, setThinkingBudgetInput] = useState("")
  const [thinkingLevelInput, setThinkingLevelInput] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function populateFrom(cfg: LlmConfigState) {
    setCurrent(cfg)
    setInput(cfg.model ?? "")
    setThinkingBudgetInput(
      cfg.extra_config && "thinking_budget" in cfg.extra_config
        ? String(cfg.extra_config.thinking_budget)
        : "",
    )
    setThinkingLevelInput(
      cfg.extra_config && "thinking_level" in cfg.extra_config ? cfg.extra_config.thinking_level : "",
    )
  }

  // Refresh after a mutation.
  const load = useCallback(async () => {
    setError(null)
    try {
      const [cfg, hist] = await Promise.all([getLlmConfig(), getLlmConfigHistory()])
      populateFrom(cfg)
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
        populateFrom(cfg)
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

  const showLevel = isGemini3Model(input)
  const extraConfig: ThinkingOverride | null = showLevel
    ? thinkingLevelInput
      ? ({ thinking_level: thinkingLevelInput } as ThinkingOverride)
      : null
    : thinkingBudgetInput.trim() !== ""
      ? { thinking_budget: Number(thinkingBudgetInput) }
      : null

  async function onSave() {
    setError(null)
    setBusy(true)
    try {
      await saveLlmConfig(input.trim(), extraConfig)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the model override.")
    } finally {
      setBusy(false)
    }
  }
```

(`onReset`, the `isSuperAdmin` early return, and the top of the JSX return down through the quick-pick chips stay exactly as they are.)

Add the thinking control right after the quick-pick chips `<div>` and before the Save/Reset buttons `<div>`:

```tsx
          <div className="space-y-1.5">
            <Label htmlFor="llm-thinking-input">
              {showLevel ? "Thinking level" : "Thinking budget"}
            </Label>
            {showLevel ? (
              <Select
                id="llm-thinking-input"
                value={thinkingLevelInput}
                onChange={(e) => setThinkingLevelInput(e.target.value)}
              >
                <option value="">No override</option>
                {THINKING_LEVELS.map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </Select>
            ) : (
              <Input
                id="llm-thinking-input"
                type="number"
                value={thinkingBudgetInput}
                onChange={(e) => setThinkingBudgetInput(e.target.value)}
                placeholder="e.g. 0 (disabled), -1 (automatic), or a token count"
              />
            )}
          </div>
```

Update the Save button's `disabled` check to pass the new `extraConfig`:

```tsx
            <Button
              onClick={onSave}
              disabled={busy || !hasPendingChange(input, extraConfig, current)}
              className="min-w-[100px]"
            >
```

(`canReset(current)` on the Reset button is unchanged.)

Add a "Thinking" column to the history table — change the header row:

```tsx
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Thinking</TableHead>
                <TableHead>Changed</TableHead>
              </TableRow>
```

Update the two placeholder rows' `colSpan` from `2` to `3`, and add the thinking cell to the data row:

```tsx
              {history?.map((row, i) => (
                <TableRow key={`${row.created_at}-${i}`}>
                  <TableCell className="font-mono text-sm">
                    {row.model ?? "Reset to default"}
                  </TableCell>
                  <TableCell className="font-mono text-sm text-muted-foreground">
                    {row.extra_config
                      ? "thinking_budget" in row.extra_config
                        ? `budget: ${row.extra_config.thinking_budget}`
                        : `level: ${row.extra_config.thinking_level}`
                      : "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {formatUpdatedAt(row.created_at)}
                  </TableCell>
                </TableRow>
              ))}
```

- [ ] **Step 2: Run the full frontend gate**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all PASS, no new errors.

- [ ] **Step 3: Manual verification**

With `vera-backend` running (`just up`, `just migrate`, `just api`) and `vera-frontend`'s dev server running (`npm run dev`):
1. Navigate to `/voice-model` as a superadmin.
2. Type a pre-3 model name (e.g. `gemini-2.5-flash`) — confirm a "Thinking budget" number input appears.
3. Type a Gemini-3 model name (e.g. `gemini-3.5-flash`) — confirm the control swaps to a "Thinking level" dropdown with `No override`/`minimal`/`low`/`medium`/`high`.
4. Save with a level set; confirm the history table's new "Thinking" column shows `level: <value>`.
5. Reset; confirm the thinking control reverts to showing no override.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/pages/LlmConfig.tsx
git commit -m "feat: add model-family-reactive thinking control to the Voice Model page"
```

---

### Task 8: Mandatory simplify pass + final verification

- [ ] **Step 1: Run the code-simplifier**

Invoke the `code-simplifier` agent (trigger phrase "simplify code") over the full diff from this plan's Tasks 1-7 (list every `Modify`/`Create` path across those tasks).

- [ ] **Step 2: Re-run the backend gate**

Run: `cd vera-backend && just check`
Expected: PASS.

- [ ] **Step 3: Re-run the frontend gate**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: PASS.

- [ ] **Step 4: Final commit (if the simplifier changed anything)**

```bash
git add -A
git commit -m "refactor: simplify thinking-config implementation"
```

(Skip this commit if the simplifier made no changes.)

- [ ] **Step 5: Final whole-branch review**

Dispatch a final code-reviewer subagent (per `superpowers:requesting-code-review`) over the full range since the parent PR's base (`d854f7a6` — the same base as the first feature's final review) through this plan's last commit, since this is a direct continuation of the same open PR (#127) rather than a separate one.
