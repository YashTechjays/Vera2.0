# Per-tenant auto-retry config from Platform Settings — design

**Date:** 2026-07-29
**Status:** approved
**Request:** Auto-retry redialed a 94%-complete form because the tenant default
`retry_fill_threshold` is 0.95. Reduce the threshold default to 0.50, and make
both the threshold and an auto-retry on/off switch per-tenant settings managed
from the platform (SUPER_ADMIN) level, following the tenant observer-toggle
pattern (PR #11, `feat/enable-disable-form-filling`).

## Decisions made during brainstorming

- **Flag layering:** auto-retry fires only when BOTH the deployment-wide
  `VERA_FORM_AUTO_RETRY_ENABLED` env var (retained as an ops kill-switch) AND
  the new per-tenant flag are on.
- **Flag default:** `Tenant.auto_retry_enabled` defaults to **False** — a
  platform operator explicitly enables it per tenant. (The test tenant needs
  one toggle after deploy to resume retries.)
- **Threshold:** model default changes `0.95 → 0.50`, and a migration
  backfills tenants still at the untouched old default
  (`WHERE retry_fill_threshold = 0.95`). Deliberately-set values are left
  alone (safe today: no admin surface for this column ever existed).
- **Surface:** platform-level only (PlatformSettings page), not the
  tenant-admin Settings page.

## Current state

- `settings.form_auto_retry_enabled` (env `VERA_FORM_AUTO_RETRY_ENABLED`,
  default False) is passed at app boot into three consumers: the worker-event
  consumer and pipeline sweeper (both reach `post_call.resolve_ai_processing`)
  and the post-call eval consumer (`EvalDeps.auto_retry_enabled` →
  `post_call_eval.evaluate_call`). It is deployment-wide; no per-tenant
  control exists.
- `Tenant.retry_fill_threshold` (`Numeric(4,3)`, NOT NULL, default 0.95) is
  already consumed per-tenant at both decision sites; there has never been an
  API/UI to set it.
- The observer-toggle pattern (to mirror): `Tenant.observer_enabled` +
  SECURITY DEFINER write fn (`platform_set_tenant_observer_enabled`, migration
  `59308656acda` — the tenant table's platform RLS policy from migration 0022
  is SELECT-only, so platform sessions cannot UPDATE directly), helper module
  `auth/platform_tenant_config.py`, endpoint
  `POST /platform/tenants/{tenant_id}/observer` gated
  `platform_require("platform:tenants:manage")` with idempotency key +
  read-back + `emit_auth_event(tenant_id=None, ...)`, `list_tenants` conditional
  disclosure, FE `PlatformSettings.tsx` optimistic Switch per tenant row,
  `lib/api/platform.ts` wrappers, integration tests in
  `test_platform_tenant_observer.py`.
- The `platform:tenants:manage` permission already exists (seeded by the
  observer PR) — reused, no new permission.

## Design

### 1. Data model & migrations

`vera_core/models/tenant.py`:

- `auto_retry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)`
  placed with the runtime knobs.
- `retry_fill_threshold` default changes `0.95 → 0.50` (column type unchanged).

Two new migrations, chained onto the current dev head (discover with
`alembic heads` at implementation time), both idempotent for fresh-CI
(create_all off live models) and provisioned DBs:

**Migration A — column + backfill + definer fn** (mirrors `59308656acda`):
1. `ALTER TABLE tenant ADD COLUMN IF NOT EXISTS auto_retry_enabled boolean NOT NULL DEFAULT false`
2. `ALTER TABLE tenant ALTER COLUMN retry_fill_threshold SET DEFAULT 0.50`
3. Backfill: `UPDATE tenant SET retry_fill_threshold = 0.50 WHERE retry_fill_threshold = 0.95`
   (0.95 = the never-admin-settable old default; runs against zero rows on
   fresh CI).
4. `platform_set_tenant_retry_config(p_tenant_id uuid, p_enabled boolean, p_threshold numeric) RETURNS boolean`
   — SECURITY DEFINER, fixed `search_path`, guarded by
   `current_setting('app.platform', true) = 'on') IS NOT TRUE` fail-closed
   check; updates both columns via
   `SET auto_retry_enabled = COALESCE(p_enabled, auto_retry_enabled), retry_fill_threshold = COALESCE(p_threshold, retry_fill_threshold)`
   so NULL params mean "unchanged" (partial update inside the fn — no
   read-merge race). Column-scoped grants
   (`SELECT (id)`, `UPDATE (auto_retry_enabled, retry_fill_threshold)`) to
   `vera_definer_owner`; owner altered to `vera_definer_owner`; EXECUTE revoked
   from PUBLIC, granted to the app role — byte-for-byte the observer fn's
   security posture.

**Migration B — widen auth-audit CHECK** for the new
`AuthEvent.TENANT_RETRY_CONFIG_UPDATED = "tenant_retry_config_updated"`
(same drop/recreate-from-`values_of(AuthEvent)` pattern as `2435e03793ff`).

### 2. Enforcement seam

The env flag keeps its boot-time plumbing and becomes the kill-switch; the
tenant flag is ANDed in at the two decision sites, both of which already hold
the tenant row:

- `control_plane/post_call.py::resolve_ai_processing`:
  `if auto_retry_enabled and tenant.auto_retry_enabled and low_fill and not user_ended:`
- `vera_core/services/post_call_eval.py::evaluate_call` (retry branch):
  `if deps.auto_retry_enabled and tenant.auto_retry_enabled:` — the
  else-branch keeps stamping `ReviewReason.AUTO_RETRY_DISABLED`.

`retry_fill_threshold` consumption is already per-tenant — unchanged.
Docstrings/comments describing the flag as "deployment-wide" are updated to
"deployment kill-switch AND per-tenant flag".

### 3. Platform API

In `api/v1/platform.py`, mirroring `set_tenant_observer`:

- `POST /platform/tenants/{tenant_id}/retry-config`
  - Body `SetTenantRetryConfigRequest`:
    `auto_retry_enabled: bool | None = None`,
    `retry_fill_threshold: float | None = Field(default=None, ge=0, le=1)` —
    at least one field required (validator raises 422 on an empty body).
  - Gates: `platform_require(TENANTS_MANAGE)` + `require_idempotency_key` +
    `claim_or_conflict`.
  - Writes through `set_tenant_retry_config(...)` (new helper beside
    `set_tenant_observer_enabled` in `auth/platform_tenant_config.py`,
    passing None for omitted fields); 404 when no tenant matched.
  - Reads BOTH values back (SELECT via the platform-readable policy) rather
    than echoing the request; responds
    `TenantRetryConfigResponse{tenant_id, auto_retry_enabled, retry_fill_threshold}`.
  - Audits `TENANT_RETRY_CONFIG_UPDATED` with
    `meta={"target_tenant": str(tenant_id), "auto_retry_enabled": ..., "retry_fill_threshold": ...}`
    (stored values — config numbers, not PHI), `tenant_id=None` like every
    platform authz record.
- `GET /platform/tenants` (`list_tenants`): `TenantSummary` gains
  `auto_retry_enabled: bool | None = None` and
  `retry_fill_threshold: float | None = None`, populated only when the caller
  holds `platform:tenants:manage` (same conditional disclosure as
  `observer_enabled`).

### 4. Frontend

- `lib/api/platform.ts`: `TenantSummary` gains the two nullable fields;
  new `setTenantRetryConfig(tenantId, patch: {auto_retry_enabled?: boolean; retry_fill_threshold?: number})`
  POSTing with an Idempotency-Key (mirror `setTenantObserverEnabled`).
- `pages/PlatformSettings.tsx`: the tenant table gains two columns —
  an "Auto retry" `Switch` (optimistic flip + revert on failure, same as the
  observer toggle) and a "Retry threshold %" numeric input (rendered 0–100,
  sent as 0–1, committed on blur/Enter with per-row saving state and error
  surfacing). Page copy extended to explain the retry semantics.

### 5. Testing

- Unit (`post_call` / `post_call_eval`): tenant flag off + env on → no requeue
  (EXCEPTION_REVIEW; eval path stamps `auto_retry_disabled`); both on →
  requeue; env off + tenant on → no requeue (kill-switch wins).
- Unit (platform endpoint, no-DB harness): partial update (one field, both,
  empty body → 422), bounds (1.5 → 422), 403 without manage, disclosure
  conditional in list_tenants.
- Integration (mirror `test_platform_tenant_observer.py`): round-trip through
  the real definer fn (flag, threshold, both, unknown tenant → 404), audit row
  with stored values, non-manager list gets None fields.
- Migration integration test (mirror the concurrency-knobs one): backfill
  0.95 → 0.50, deliberately-set 0.80 untouched, `auto_retry_enabled` lands
  false.
- Existing tests touched: any relying on `retry_fill_threshold=0.95` default
  or on auto-retry firing with only the env flag get the tenant flag set
  explicitly.
- Gates: backend `just check`; frontend `tsc -b` + `eslint` + tests + build.

## Risks & trade-offs

- Post-deploy, no tenant auto-retries until a platform operator flips the
  toggle (chosen default) — the test tenant needs one click.
- The 0.95→0.50 backfill assumes 0.95 means "never touched"; true today.
- The definer fn's COALESCE partial-update means an all-NULL call is a no-op
  that still returns true for a matching tenant — the endpoint's
  at-least-one-field validation prevents ever issuing it.

## Out of scope

- Tenant-admin (non-platform) surface for these knobs.
- Removing `VERA_FORM_AUTO_RETRY_ENABLED` (stays as kill-switch).
- Changing `max_retries` handling or retry-scope (focused/fresh) behavior.
