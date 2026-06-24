# Persona Tweak — Runtime Knob Wiring — Design

**Date:** 2026-06-23
**Status:** Draft, pending review
**Related:** `vera_core.models.tenant.Tenant` (Fig 7 runtime knobs), `apps/agent_worker` cascade,
`apps/control_plane` display-path chain (`apps/control_plane/src/control_plane/CLAUDE.md`),
`vera_core.observability.correlation` (room name → tenant_id).

## Problem

`Tenant` carries three runtime knobs from the spec (Fig 7) so behaviour is tenant
config, not code: `max_agents_per_va`, `retry_fill_threshold`, `persona_tweak`. All
three are defined on the model (`models/tenant.py:40-42`) but **nothing reads them** —
they are inert columns today.

This spec wires **`persona_tweak`** end-to-end: a tenant admin can set it via the
control plane, and the agent worker applies it to the agent's persona for that
tenant's calls. The other two knobs are explicitly out of scope (separate tasks).

## Goal & scope

> A tenant admin `PUT`s a `persona_tweak` for their tenant. The next call the control
> plane starts loads that tweak, ships it to the worker via LiveKit dispatch metadata,
> and the worker builds the agent's instructions with the tweak applied — without the
> worker ever touching Postgres.

**In scope:**
- `PersonaTweak` validated schema in `vera_core.schemas` (shared by both apps).
- Tenant-admin `GET`/`PUT` endpoints for `persona_tweak`, audited, RBAC-gated.
- Control plane loads `persona_tweak` at `start_call` and passes it as dispatch metadata.
- Worker parses dispatch metadata and applies the tweak when building agent instructions.

**Out of scope:** `max_agents_per_va`, `retry_fill_threshold`; any frontend work;
full-prompt override or `tone` swapping (see Decisions).

## Decisions

- **Transport: dispatch metadata.** The worker has no Postgres access by design (control
  plane owns Postgres; worker is LiveKit-only). The control plane already holds a
  `TenantSession` + `tenant_id` at `start_call`, so it loads the tweak and serializes it
  into `CreateAgentDispatchRequest(metadata=...)`. The worker reads `ctx.job.metadata`.
  This keeps the worker DB-free and matches the existing architecture.
- **Tweak shape: typed fields, append-only overlay.** `persona_tweak` is a small validated
  model, `extra="forbid"`. Initial fields:
  - `extra_instructions: str | None` — appended to the base `SYSTEM_PROMPT`.
  - `greeting: str | None` — overrides the base `GREETING`.
  Each field length-capped to bound prompt growth. `tone` and full-prompt override are
  **dropped** (YAGNI): `extra_instructions` covers the same need without authoring swap
  content or letting a tenant break core verification behaviour. The model is the seam to
  extend later.
- **Empty tweak is a no-op.** `{}` (the column default) → base prompt unchanged.
- **Fail-safe parse in the worker.** Missing/malformed/invalid dispatch metadata → empty
  tweak (base persona), logged. A bad tweak must never crash a live call. (Mirrors the
  cascade's existing fail-safe posture; distinct from the strict PHI seams.)
- **Permission: new `tenant:config:manage`.** `tenant:auth:configure` is auth-specific;
  runtime-config knobs get their own gate so the two grants stay independent.

## Schema (`vera_core.schemas`)

```python
class PersonaTweak(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_instructions: str | None = Field(default=None, max_length=4000)
    greeting: str | None = Field(default=None, max_length=500)
```

Importable by both `control_plane` (validate on write, serialize on dispatch) and
`agent_worker` (parse on call start). The `Tenant.persona_tweak` JSONB stores the
model's `.model_dump(exclude_none=True)`; the empty dict is the documented default.

**PHI note:** `persona_tweak` is admin-authored, non-PHI configuration. It may travel in
dispatch metadata and enter the LLM prompt — both permitted for non-PHI config. The
endpoint is non-PHI, so no `phi:read` gate and no PHI-access audit; it is still audited as
a tenant-config mutation.

## Runtime path (control plane → worker)

1. **`prompt.py`** — `build_instructions(tweak: PersonaTweak) -> str` applies overrides on
   top of the base constants: append `extra_instructions` after `SYSTEM_PROMPT` (before /
   alongside `CARTESIA_MARKUP_GUIDE`). Add a `resolve_greeting(tweak) -> str` (or fold into
   the same builder) so `GREETING` is tweak-aware. The base `_INSTRUCTIONS` constant stops
   being the live path.
2. **`agent.py`** — `VeraAgent.__init__` takes resolved `instructions: str` and
   `greeting: str` (constructed per call) instead of importing `_INSTRUCTIONS`/`GREETING`
   directly. `on_enter` says the resolved greeting.
3. **`livekit_gateway.py`** — `create_call_room(room_name, metadata: str = "")` passes
   `metadata` into `CreateAgentDispatchRequest(metadata=metadata)`.
4. **`calls.py` `start_call`** — load the `Tenant` row (RLS-scoped), parse its
   `persona_tweak` into `PersonaTweak`, serialize to JSON, pass as dispatch metadata.
5. **`main.py` entrypoint** — parse `ctx.job.metadata` → `PersonaTweak` (fail-safe to empty
   on missing/invalid), build instructions + greeting, pass to `VeraAgent`.

## API path (read/update the knob)

Tenant-scoped, follows the control-plane display-path chain, gated by
`tenant:config:manage`:
- `GET  /tenants/{tenant_slug}/config/persona` → current `persona_tweak` as `PersonaTweak`.
- `PUT  /tenants/{tenant_slug}/config/persona` → validate body against `PersonaTweak`,
  persist `.model_dump(exclude_none=True)` on the tenant row, audit the change, return the
  stored value. Uses `ResponseModel[T]` / `ok(...)`, `CustomAPIException` errors,
  `Cache-Control: no-store`.

The `tenant:config:manage` permission is added to the RBAC catalog and granted to
`TENANT_ADMIN`, mirroring how `tenant:auth:configure` is wired.

## Testing

- **Unit:** `PersonaTweak` validation (unknown keys rejected, length caps enforced);
  `build_instructions`/greeting resolution for empty, partial, full tweak; worker metadata
  parse including the malformed-metadata fail-safe path.
- **Integration:** `PUT` then `GET` round-trip under RLS; permission denial for a caller
  without `tenant:config:manage`; cross-tenant isolation; `start_call` surfaces the stored
  tweak into dispatch metadata.

## Components & boundaries

- `vera_core.schemas.PersonaTweak` — the single shared contract; the only thing both apps
  import to agree on shape.
- `agent_worker.prompt` — pure functions: `(base constants, tweak) -> instructions/greeting`.
  No I/O; trivially unit-testable.
- `control_plane` config endpoint — owns persistence + audit + RBAC; never builds prompts.
- `livekit_gateway` — transport only; opaque metadata string, no knowledge of tweak shape.
