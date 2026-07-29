# Dynamic runtime override of the voice cascade's LLM model

**Date:** 2026-07-23
**Status:** Approved for implementation
**Related:** `apps/agent_worker/src/agent_worker/cascade.py` (hardcodes
`google.LLM(model="gemini-2.5-flash", ...)` today)

## Problem

The voice cascade's LLM model is a Python literal in `cascade.py`. Changing it
today means editing code, redeploying the agent worker, and restarting every
worker process — there is no way for a platform operator to try a different
Gemini model, or roll one back, without a deploy. There is also no DB shape
that could later support the same kind of override for the STT (Deepgram) or
TTS (Cartesia) stages, or for switching providers rather than just model names.

## Decision (user-confirmed)

1. **Scope: LLM model name only, Google provider only, platform-wide (global),
   this iteration.** No provider switching, no per-tenant override — a single
   active value applies to every tenant's calls. The DB shape must generalize
   to STT/TTS and to provider changes later, without a redesign, even though
   only the `llm` stage is wired end-to-end now.
2. **Freeform model name.** Four values are suggested in the UI (`gemini-2.5-flash`,
   `gemini-3.1-flash-lite`, `gemini-3.5-flash`, `gemini-3.6-flash`) but any
   string the operator types is accepted.
3. **Basic validation only — no live Vertex AI check.** control_plane's prod
   service account does not yet have Vertex AI IAM permissions (`adr/devops-todo.md`
   row 13 is still open — only agent_worker's SA is granted `roles/aiplatform.user`
   today). Adding a live-verify call would depend on infra that isn't provisioned.
   Save-time validation is therefore: trimmed, non-empty, reasonable max length,
   conservative charset. An invalid-but-well-formed model name fails at call
   time (worker logs / call failure), exactly as a bad hardcoded value would today.
4. **Keep full history**, append-only: every save or reset is a new row, nothing
   is ever updated in place. "Current effective value" is simply the newest row
   for a stage.
5. **Explicit "reset to default"** action, distinct from typing the hardcoded
   model name back in — clears the override and records who did it and when.
6. **New dedicated permissions**, not reused from an existing resource:
   `platform:llm_config:read` / `platform:llm_config:write`, seeded via
   migration and granted to `SUPER_ADMIN` only (mirrors
   `f503e82734cc_seed_form_schemas_read_permission.py` exactly — a brand-new
   capability, not a backfill).

## Design

### 1. Data model — `voice_model_config` (`vera_core/models/`)

A global table (no `tenant_id`, no RLS — same treatment as `Prompt`/`FormSchema`),
purely append-only (`CreatedAtMixin`, never `UPDATE`d — no `is_active` flag; the
newest row per `stage` **is** the current value, ordering does all the work
history and "current" both need):

```python
class VoiceModelConfig(UUIDv7PKMixin, CreatedAtMixin, Base):
    __tablename__ = "voice_model_config"

    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("stage IN ('stt', 'llm', 'tts')", name="stage_valid"),
        CheckConstraint(
            "(model IS NULL AND provider IS NULL) OR (model IS NOT NULL AND provider IS NOT NULL)",
            name="model_provider_pair",
        ),
        Index("ix_voice_model_config_stage_created_at", "stage", "created_at"),
    )
```

`model IS NULL` (paired with `provider IS NULL`) is the explicit "reset to
default" state — a real, queryable row, not an absence of rows. "Current
effective config" for a stage is:

```sql
SELECT * FROM voice_model_config WHERE stage = :stage ORDER BY created_at DESC, id DESC LIMIT 1
```

No row at all, or a latest row with `model IS NULL` → the hardcoded fallback
applies. Only `stage='llm'` is ever written this iteration; `stt`/`tts` are
schema-ready but unused (no seeding, no read path) until a future iteration
wires them up.

**Migration** (new table, so it needs the same idempotent treatment CLAUDE.md
requires for columns — `0001`'s `create_all()` runs against *current* models
at runtime, so a fresh DB already has this table by the time this migration
runs; an already-provisioned DB does not):

```python
def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS voice_model_config (
            id UUID PRIMARY KEY,
            stage VARCHAR(16) NOT NULL,
            provider VARCHAR(64),
            model VARCHAR(200),
            created_by_user_id UUID REFERENCES app_user(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "DO $$ BEGIN "
        "ALTER TABLE voice_model_config ADD CONSTRAINT ck_voice_model_config_stage_valid "
        "CHECK (stage IN ('stt', 'llm', 'tts')); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "ALTER TABLE voice_model_config ADD CONSTRAINT ck_voice_model_config_model_provider_pair "
        "CHECK ((model IS NULL AND provider IS NULL) OR (model IS NOT NULL AND provider IS NOT NULL)); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_voice_model_config_stage_created_at "
        "ON voice_model_config (stage, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_model_config")
```

### 2. Permissions — new migration, mirrors `f503e82734cc`

- `PLATFORM_PERMISSIONS` in `vera_core/models/rbac_defaults.py` gains:
  ```python
  "platform:llm_config:read": "View the active voice cascade LLM model override",
  "platform:llm_config:write": "Set or reset the voice cascade LLM model override",
  ```
  (`SUPER_ADMIN = frozenset(ALL_PERMISSIONS)` picks these up automatically for
  newly-provisioned DBs.)
- A dedicated migration seeds both permission rows and grants both straight to
  the global `SUPER_ADMIN` role — same shape as
  `f503e82734cc_seed_form_schemas_read_permission.py` (`INSERT ... ON CONFLICT
  (code) DO NOTHING` for `permission`, then `INSERT ... SELECT ... FROM role r,
  permission p WHERE r.name = 'SUPER_ADMIN' ... ON CONFLICT (role_id,
  permission_id) DO NOTHING` for `role_permission`). `downgrade()` raises
  (grants are indistinguishable from live usage by then), same as the precedent.

### 3. control_plane endpoints — `api/v1/llm_config.py`

Standard shape from `control_plane/CLAUDE.md`: `ResponseModel[T]` via `ok()`,
`CustomAPIException` on error, mutating routes behind
`Depends(require_idempotency_key)` + `claim_or_conflict`.

- `GET /api/v1/platform/llm-config` — `platform_require("platform:llm_config:read")`.
  Returns the current effective row: `{provider, model, is_default, created_at,
  created_by}` (`is_default = model is None`).
- `GET /api/v1/platform/llm-config/history` — same read permission. Full
  history for `stage='llm'`, newest first.
- `PUT /api/v1/platform/llm-config` — `platform_require("platform:llm_config:write")`.
  Body `{model: str}`. Validates trimmed/non-empty/max-length/conservative
  charset (422 on failure), inserts `{stage: "llm", provider: "google", model:
  <normalized>, created_by_user_id: <caller>}`.
- `POST /api/v1/platform/llm-config/reset` — same write permission. No-op
  (200, no new row) if already at default; otherwise inserts `{stage: "llm",
  provider: NULL, model: NULL, created_by_user_id: <caller>}`.

### 4. Delivery to the DB-free agent worker — reuse the `persona_tweak` channel

agent_worker never opens a DB session — it only reads LiveKit job dispatch
`metadata` and the Redis-staged `CallPlan`. `persona_tweak` already flows via
metadata regardless of whether a call uses a `CallPlan`, so the model override
follows the same channel rather than riding on `CallPlan` (which is absent for
some calls, e.g. Voice Lab sandbox sessions).

Both call sites that build dispatch `metadata` before calling
`LiveKitGateway.create_call_room` — `queue_dispatcher.py::try_dispatch` (real
queued calls) and `voice_lab.py` (sandbox calls) — call a new shared helper,
mirroring `ivr_selection.py`'s `add_active_playbook_metadata`:

```python
# vera_core/services/model_config.py
async def add_llm_model_override_metadata(
    session: AsyncSession, metadata: dict[str, Any]
) -> None:
    """Newest voice_model_config row for stage='llm', if its model is set, rides
    dispatch metadata as `llm_model_override`; a missing key means "use the
    hardcoded default" — mirrors add_active_playbook_metadata's missing-key
    convention."""
    row = (
        await session.execute(
            select(VoiceModelConfig.model)
            .where(VoiceModelConfig.stage == "llm")
            .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row:
        metadata["llm_model_override"] = row
```

Called once per dispatch, right alongside where `persona_tweak` is added
(`queue_dispatcher.py:358-359`) and in `voice_lab.py` before its own
`create_call_room` call. Read fresh every dispatch, no caching — same
convention as `Tenant.persona_tweak`.

### 5. agent_worker — `main.py` + `cascade.py`

`main.py`'s `entrypoint()` reads the new key alongside the existing ones:

```python
meta = json.loads(ctx.job.metadata or "{}")
...
session = build_session(
    vad=ctx.proc.userdata.get("vad"),
    key_terms=controller.plan.stt_key_terms if controller is not None else None,
    llm_model=meta.get("llm_model_override"),
)
```

`cascade.py` names the fallback instead of leaving it an inline literal, and
accepts the override:

```python
_DEFAULT_LLM_MODEL = "gemini-2.5-flash"

def build_session(
    vad: Any | None = None,
    *,
    key_terms: list[str] | None = None,
    llm_model: str | None = None,
) -> AgentSession[TakeoverState]:
    return AgentSession(
        ...
        llm=google.LLM(
            model=llm_model or _DEFAULT_LLM_MODEL,
            vertexai=True,
            location="global",
            thinking_config=ThinkingConfig(thinking_budget=0),
        ),
        ...
    )
```

In-flight calls are unaffected by a later save — only calls dispatched
afterward pick up the new value. No hot-swap mid-call; this is intentional.

### 6. Frontend

- `src/pages/LlmConfig.tsx`, pattern-matched off `InsuranceProviders.tsx`:
  gate on `selectIsSuperAdmin` (defense-in-depth; the backend enforces via
  `platform_require`), plain `useState`/`useEffect` fetch (no react-query),
  shadcn/Radix components.
- Shows the current effective value with a badge ("Override" vs "Default"),
  a text `Input` with the 4 suggested values as quick-pick chips beneath it
  (freeform still allowed), a **Save** button, a **Reset to default** button
  (disabled when already at default), and a history list (model, provider,
  changed by, changed at).
- `src/lib/api/llmConfig.ts` — thin wrapper over `apiRequest<T>`, one function
  per endpoint (`getLlmConfig`, `getLlmConfigHistory`, `saveLlmConfig`,
  `resetLlmConfig`).
- New `nav.ts` entry gated on `platform:llm_config:read`, route registered in
  `App.tsx` alongside the other super-admin-only pages, wrapped in
  `RequireNavRoute`.

## Error handling

- Save/reset: 422 on failed basic validation (empty, too long, bad charset).
- Reset when already at default: 200, no-op, no new row.
- Dispatch-time read (`add_llm_model_override_metadata`) failing for any
  reason must not block a call from being placed — log (no PHI) and leave
  `metadata` untouched (worker falls back to the hardcoded default).
- An overridden-but-invalid model name fails at call time the same way a bad
  hardcoded value would today — no special handling, per the basic-validation-only
  decision.

## Testing

- Backend: endpoint tests (permission-gating on both new permissions,
  save/reset/history behavior, validation edge cases); a unit test asserting
  "current effective config" always resolves to the newest row; a
  dispatch-path test confirming `metadata["llm_model_override"]` is set when
  a row exists and absent otherwise, for both `try_dispatch` and `voice_lab.py`.
- agent_worker: `build_session` honors an explicit `llm_model` override vs.
  falls back to `_DEFAULT_LLM_MODEL` when `None`.
- Frontend: page-level test for load/save/reset flows and permission-gated
  rendering.
- Manual: boot `control_plane` + `agent_worker` locally (`just up`, `just
  migrate`, `just api`, `just worker`), set an override via the new page,
  place a real call, confirm via worker logs (no PHI) that the session was
  built with the overridden model string.

## Out of scope

- STT (Deepgram) and TTS (Cartesia) stage overrides — the table supports them
  (`stage IN ('stt','llm','tts')`) but no seeding, no read path, no UI this
  iteration.
- Provider switching (e.g. LLM provider other than Google) — `provider` is
  always written as `"google"` by the write endpoint; a different provider
  needs new `cascade.py` code regardless of what the DB holds.
- Per-tenant overrides — single global value only.
- Live Vertex AI validation of the model name at save time — blocked on the
  open `adr/devops-todo.md` IAM TODO for control_plane's service account;
  revisit once that's provisioned.
- Mid-call hot-swap of the LLM model for an in-flight session.
