# Per-Tenant Auto-Retry Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Platform operators can enable/disable auto-retry and set the retry fill threshold per tenant; the threshold default drops to 0.50; the env var stays as a deployment kill-switch ANDed with the tenant flag.

**Architecture:** Mirror the tenant observer-toggle pattern end to end: a `Tenant.auto_retry_enabled` column (default False) written through a new `platform_set_tenant_retry_config` SECURITY DEFINER function (platform RLS on `tenant` is SELECT-only), a `POST /platform/tenants/{id}/retry-config` endpoint gated by the existing `platform:tenants:manage` permission, conditional disclosure in `list_tenants`, and two new controls on the PlatformSettings page. The two retry decision sites (`post_call.resolve_ai_processing`, `post_call_eval.evaluate_call`) AND the tenant flag with the env kill-switch they already receive.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + pytest (mypy --strict, ruff); React + Vite + TS + vitest (jsdom/RTL infra exists).

**Spec:** `docs/superpowers/specs/2026-07-29-tenant-auto-retry-config-design.md`

## Global Constraints

- Branch: `feat/tenant-auto-retry-config` (created off dev; spec committed).
- Backend gate: `just check` verbatim (never a subset). Frontend gate: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build`. Frontend lockfile changes (none expected) must use the pinned npm 10.9.8.
- Migrations: alembic-generated random-hex IDs (`uv run alembic revision -m "..."`); idempotent for BOTH fresh-CI (0001 `create_all` off live models already materializes new columns) and provisioned DBs; chain onto the current head (`uv run alembic heads` → `2435e03793ff` at plan time — re-check before generating, dev moves fast).
- Definer-fn security posture copied exactly from migration `20260723_1520_59308656acda_tenant_observer_enabled_and_definer.py`: fixed search_path, `app.platform` GUC fail-closed check, owner `vera_definer_owner`, column-scoped grants, EXECUTE revoked from PUBLIC and granted to the app role.
- Audit: `emit_auth_event` only, `tenant_id=None` on platform routes, meta carries config values (ints/bools/floats — no PHI).
- Bounds verbatim: `retry_fill_threshold` ge=0 le=1. Tenant flag default False. Threshold default 0.50; backfill only `WHERE retry_fill_threshold = 0.95`.
- Comments one line, non-obvious constraints only; docstrings short; timestamps via DB clock.
- Local docker infra is up; shared `vera_test` DB is healthy on dev's migration graph. After Task 1's migration lands, run `just migrate` and re-run `DROP DATABASE vera_test`-style refresh only if conftest complains (it migrates fresh test DBs automatically).

---

### Task 1: Model changes + migration (column, threshold default, backfill, definer fn) + migration test

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/tenant.py:38-48`
- Create: `vera-backend/migrations/versions/<generated>_tenant_auto_retry_config.py`
- Test: `vera-backend/tests/integration/db/test_tenant_auto_retry_config_migration.py`

**Interfaces:**
- Produces: `Tenant.auto_retry_enabled: Mapped[bool]` (NOT NULL default False) and `retry_fill_threshold` default 0.50 — read by Tasks 2–4. SQL fn `platform_set_tenant_retry_config(uuid, boolean, numeric) RETURNS boolean` — called by Task 3's helper.

- [ ] **Step 1: Model edits**

In `tenant.py`, below `retry_fill_threshold` (line ~44), and change its default:

```python
    retry_fill_threshold: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0.50)
    # Per-tenant auto-retry switch, ANDed with the deployment kill-switch
    # (VERA_FORM_AUTO_RETRY_ENABLED); platform-managed, off until enabled.
    auto_retry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

Update the comment above `retry_fill_threshold` (line ~38) if it names 0.95, and the class docstring knob list (line ~16) to include `auto_retry_enabled`.

- [ ] **Step 2: Generate the migration**

```bash
cd vera-backend && uv run alembic heads   # confirm the single current head
uv run alembic revision -m "tenant auto retry config"
```

- [ ] **Step 3: Write the migration body**

Model the file header/imports on `20260723_1520_59308656acda_tenant_observer_enabled_and_definer.py` (read it first — same `DEFINER_ROLE`/`_APP_ROLE` constants and grant choreography):

```python
DEFINER_ROLE = "vera_definer_owner"
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"
_SIGNATURE = "platform_set_tenant_retry_config(uuid, boolean, numeric)"

_SET_TENANT_RETRY_CONFIG = """
CREATE OR REPLACE FUNCTION platform_set_tenant_retry_config(
    p_tenant_id uuid,
    p_enabled boolean,
    p_threshold numeric
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_count bigint;
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_set_tenant_retry_config: not a platform session';
    END IF;

    -- NULL params mean "leave unchanged": partial update inside the fn, no
    -- read-merge race between a SELECT and a separate write.
    UPDATE tenant
       SET auto_retry_enabled = COALESCE(p_enabled, auto_retry_enabled),
           retry_fill_threshold = COALESCE(p_threshold, retry_fill_threshold)
     WHERE id = p_tenant_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS auto_retry_enabled boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE tenant ALTER COLUMN retry_fill_threshold SET DEFAULT 0.50")
    # 0.95 = the never-admin-settable old default (no API/UI ever wrote this column),
    # so equality identifies untouched rows; deliberately-set values are left alone.
    op.execute("UPDATE tenant SET retry_fill_threshold = 0.50 WHERE retry_fill_threshold = 0.95")
    op.execute(f"GRANT SELECT (id) ON tenant TO {DEFINER_ROLE}")
    op.execute(f"GRANT UPDATE (auto_retry_enabled, retry_fill_threshold) ON tenant TO {DEFINER_ROLE}")
    op.execute(_SET_TENANT_RETRY_CONFIG)
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {DEFINER_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM {_APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO PUBLIC")
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
    op.execute(f"REVOKE UPDATE (auto_retry_enabled, retry_fill_threshold) ON tenant FROM {DEFINER_ROLE}")
    op.execute("ALTER TABLE tenant ALTER COLUMN retry_fill_threshold SET DEFAULT 0.95")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS auto_retry_enabled")
```

Note: no `UPGRADE_STATEMENTS` tuple here — the definer-fn migrations don't use that convention (they parametrize `_APP_ROLE`); the migration test drives the pieces it needs directly (Step 4), mirroring how `test_tenant_concurrency_knobs_migration.py` isolates the backfill.

- [ ] **Step 4: Write the migration test**

Create `tests/integration/db/test_tenant_auto_retry_config_migration.py`, modeled structurally on `tests/integration/db/test_tenant_concurrency_knobs_migration.py` (same never-committed-transaction pattern, same glob-for-migration-file trick — glob `*_tenant_auto_retry_config.py`). Because this migration has no `UPGRADE_STATEMENTS` tuple, extract the two statements under test as module constants in the migration (`BACKFILL_THRESHOLD = "UPDATE tenant SET retry_fill_threshold = 0.50 WHERE retry_fill_threshold = 0.95"` used by `upgrade()` and imported by the test — one source of truth). Tests:

```python
async def test_threshold_backfill_rewrites_untouched_default(...):
    # Tenant(retry_fill_threshold=0.95) → after BACKFILL_THRESHOLD runs → 0.50

async def test_threshold_backfill_leaves_deliberate_value(...):
    # Tenant(retry_fill_threshold=0.80) → unchanged 0.80

async def test_definer_fn_partial_update(...):
    # Requires the migrated fn (conftest DB is at head). Inside a platform-GUC
    # session (SET app.platform = 'on'; see auth/platform_tenant_config usage):
    # call platform_set_tenant_retry_config(id, true, NULL) → flag flips,
    # threshold untouched; (id, NULL, 0.42) → threshold set, flag untouched;
    # unknown uuid → returns false.
```

For the definer test use `SELECT set_config('app.platform', 'on', true)` on the session before invoking the fn (`SET LOCAL` equivalent — look at how `tests/integration/control_plane/test_platform_tenant_observer.py` exercises its fn and copy that mechanism if it differs).

- [ ] **Step 5: Run migration + tests**

```bash
cd vera-backend && just migrate
uv run pytest tests/integration/db/test_tenant_auto_retry_config_migration.py -q
uv run pytest tests/unit -q   # smoke: model default changes ripple nowhere unexpected
```

Expected: all green; `just migrate` re-run is a no-op (idempotency).

- [ ] **Step 6: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/tenant.py vera-backend/migrations/versions/ vera-backend/tests/integration/db/test_tenant_auto_retry_config_migration.py
git commit -m "feat(tenant): auto_retry_enabled column, 0.50 threshold default+backfill, retry-config definer fn"
```

---

### Task 2: Enforcement — AND the tenant flag at both retry decision sites

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/post_call.py:95` (+ docstrings 8-14, 60-62)
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/post_call_eval.py:501` (+ `EvalDeps` comment ~81)
- Test: `vera-backend/tests/unit/control_plane/test_post_call.py`
- Test: `vera-backend/tests/unit/control_plane/test_worker_events.py` (tenant dict defaults)
- Test: `vera-backend/tests/integration/test_post_call_eval.py` (seed tenants need the flag)

**Interfaces:**
- Consumes: `Tenant.auto_retry_enabled` (Task 1).
- Produces: the runtime semantics Tasks 3–4 describe in UI copy — retry fires only when env kill-switch AND tenant flag are both on.

- [ ] **Step 1: Write the failing unit tests**

In `tests/unit/control_plane/test_post_call.py`: add `"auto_retry_enabled": True,` to the `_tenant` defaults dict (existing requeue tests then keep passing — they already pass `auto_retry_enabled=True` as the env flag), then add:

```python
@pytest.mark.asyncio
async def test_low_completion_with_tenant_flag_off_goes_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env kill-switch on but the tenant's own flag off → no requeue: the
    per-tenant flag is ANDed, not overridden, by the deployment switch."""
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=40.0, retry_count=0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id, auto_retry_enabled=False),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(
        _SM, audit, ref, trigger="call.ended", auto_retry_enabled=True
    )

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.retry_count == 0
```

Run: `uv run pytest tests/unit/control_plane/test_post_call.py -q` — new test FAILS (form requeues today).

- [ ] **Step 2: Implement the gate change**

`post_call.py:95`:

```python
        if auto_retry_enabled and tenant.auto_retry_enabled and low_fill and not user_ended:
```

Update the module docstring (lines 8-14) and the function docstring (60-62): the edge is "feature-gated behind the deployment kill-switch (`settings.form_auto_retry_enabled`) AND the tenant's `auto_retry_enabled`".

`post_call_eval.py:501`:

```python
        if deps.auto_retry_enabled and tenant.auto_retry_enabled:
```

(`tenant` is already in scope — it's loaded for `max_retries` at line ~491.) Update the `EvalDeps.auto_retry_enabled` comment (~line 81): "the deployment kill-switch; ANDed with tenant.auto_retry_enabled at the decision site". The else-branch already stamps `ReviewReason.AUTO_RETRY_DISABLED` — now also correct for tenant-flag-off; extend its comment accordingly.

- [ ] **Step 3: Fix the ripple in existing tests**

- `tests/unit/control_plane/test_worker_events.py`: add `"auto_retry_enabled": True,` to its tenant defaults dict (line ~306) — its two auto-requeue tests (638, 698) rely on the requeue path.
- `tests/integration/test_post_call_eval.py`: the `_seed_form` helper creates the `Tenant` row — seed `auto_retry_enabled=True` there (the requeue tests pass `EvalDeps(auto_retry_enabled=True)` and must keep requeueing; the `auto_retry_disabled` test keeps `deps` flag False so it still parks). Add one new integration test mirroring `test_incomplete_retryable_with_auto_retry_disabled_goes_to_review` but with deps flag True and the TENANT flag False (update the seeded tenant row in-test), asserting `review_reason == "auto_retry_disabled"`.

- [ ] **Step 4: Run the affected suites**

```bash
cd vera-backend && uv run pytest tests/unit/control_plane/test_post_call.py tests/unit/control_plane/test_worker_events.py tests/integration/test_post_call_eval.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend/apps/control_plane/src/control_plane/post_call.py vera-backend/packages/vera_core/src/vera_core/services/post_call_eval.py vera-backend/tests/
git commit -m "feat(retry): per-tenant auto_retry_enabled ANDed with the deployment kill-switch"
```

---

### Task 3: Platform API — retry-config endpoint, list_tenants disclosure, audit event

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/enums.py` (below `TENANT_OBSERVER_UPDATED`)
- Create: `vera-backend/migrations/versions/<generated>_widen_auth_audit_event_for_tenant_retry.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/auth/platform_tenant_config.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/platform.py:180-292`
- Test: `vera-backend/tests/integration/control_plane/test_platform_tenant_retry_config.py` (create)

**Interfaces:**
- Consumes: Task 1's definer fn; existing `TENANTS_MANAGE`, `platform_require`, `PlatformSession`, `require_idempotency_key`, `claim_or_conflict`, `PLATFORM_IDEM_SCOPE`.
- Produces: `POST /api/v1/platform/tenants/{tenant_id}/retry-config` accepting `{auto_retry_enabled?, retry_fill_threshold?}` → `{tenant_id, auto_retry_enabled, retry_fill_threshold}`; `TenantSummary` gains both fields (nullable) — consumed by Task 4.

- [ ] **Step 1: AuthEvent + widen migration**

`enums.py`, below `TENANT_OBSERVER_UPDATED`:

```python
    # Platform operator changed a tenant's auto-retry config (flag/threshold values, no PHI).
    TENANT_RETRY_CONFIG_UPDATED = "tenant_retry_config_updated"
```

Generate a migration `widen auth audit event for tenant retry` copying `20260723_1522_2435e03793ff_widen_auth_audit_event_for_tenant_observer.py` byte-for-byte in structure (drop/recreate the CHECK from `values_of(AuthEvent)`).

- [ ] **Step 2: Definer write helper**

Append to `auth/platform_tenant_config.py` (module docstring gains one line naming the second fn):

```python
async def set_tenant_retry_config(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    enabled: bool | None,
    threshold: float | None,
) -> bool:
    """Update a tenant's auto-retry flag/threshold via the platform definer fn
    (NULL = leave unchanged). Returns True when a tenant row matched."""
    result = await session.execute(
        text(
            "SELECT platform_set_tenant_retry_config("
            "CAST(:tenant_id AS uuid), :enabled, :threshold)"
        ).bindparams(tenant_id=tenant_id, enabled=enabled, threshold=threshold)
    )
    return bool(result.scalar_one())
```

- [ ] **Step 3: Endpoint + list_tenants (write the failing integration tests first)**

Create `tests/integration/control_plane/test_platform_tenant_retry_config.py` by copying `test_platform_tenant_observer.py`'s world fixture wholesale (its module docstring notes there is no shared platform conftest yet — same local `observer_world`-style fixture, renamed `retry_world`). Tests:

```python
async def test_list_tenants_discloses_retry_config_to_manager(...):
    # GET /api/v1/platform/tenants → rows carry auto_retry_enabled (False) and
    # retry_fill_threshold (0.50) for the manager persona.

async def test_set_flag_only_persists_and_audits(...):
    # POST {"auto_retry_enabled": true} → 200 {tenant_id, true, 0.50};
    # DB row flipped, threshold untouched; auth_audit row
    # event_type == "tenant_retry_config_updated",
    # meta == {"target_tenant": ..., "auto_retry_enabled": True,
    #          "retry_fill_threshold": 0.5}.

async def test_set_threshold_only(...):
    # POST {"retry_fill_threshold": 0.42} → flag untouched, threshold 0.42.

async def test_empty_body_is_422(...)
async def test_out_of_bounds_threshold_is_422(...)   # 1.5
async def test_unknown_tenant_is_404(...)
async def test_non_manager_is_403_and_list_hides_values(...)
```

Run — all fail (route missing). Then implement in `platform.py` directly below `set_tenant_observer`, mirroring it exactly:

```python
class SetTenantRetryConfigRequest(BaseModel):
    auto_retry_enabled: bool | None = None
    retry_fill_threshold: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "SetTenantRetryConfigRequest":
        if self.auto_retry_enabled is None and self.retry_fill_threshold is None:
            raise ValueError("provide auto_retry_enabled and/or retry_fill_threshold")
        return self


class TenantRetryConfigResponse(BaseModel):
    tenant_id: UUID
    auto_retry_enabled: bool
    retry_fill_threshold: float


@router.post(
    "/tenants/{tenant_id}/retry-config",
    response_model=ResponseModel[TenantRetryConfigResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def set_tenant_retry_config_endpoint(
    tenant_id: UUID,
    body: SetTenantRetryConfigRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantRetryConfigResponse]:
    """Set a tenant's auto-retry flag and/or fill threshold. Writes through the
    platform_set_tenant_retry_config SECURITY DEFINER fn (the tenant table's
    platform RLS policy is SELECT-only), audited null-tenant like every other
    /platform authz."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    matched = await set_tenant_retry_config(
        session,
        tenant_id=tenant_id,
        enabled=body.auto_retry_enabled,
        threshold=body.retry_fill_threshold,
    )
    if not matched:
        raise NotFoundError(message="no such tenant")
    # Read the values BACK rather than echoing the request (same rationale as
    # set_tenant_observer: the fn only reports whether a row matched).
    row = (
        await session.execute(
            select(Tenant.auto_retry_enabled, Tenant.retry_fill_threshold).where(
                Tenant.id == tenant_id
            )
        )
    ).one()
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_RETRY_CONFIG_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={
            "target_tenant": str(tenant_id),
            "auto_retry_enabled": bool(row.auto_retry_enabled),
            "retry_fill_threshold": float(row.retry_fill_threshold),
        },
    )
    return ok(
        TenantRetryConfigResponse(
            tenant_id=tenant_id,
            auto_retry_enabled=bool(row.auto_retry_enabled),
            retry_fill_threshold=float(row.retry_fill_threshold),
        )
    )
```

Imports: extend the existing `from control_plane.auth.platform_tenant_config import ...` line with `set_tenant_retry_config`; `model_validator`/`Field` from pydantic if not present.

`TenantSummary` (line ~180) gains, with the same tri-state comment as `observer_enabled`:

```python
    auto_retry_enabled: bool | None = None
    retry_fill_threshold: float | None = None
```

`list_tenants` SELECT adds `Tenant.auto_retry_enabled, Tenant.retry_fill_threshold`; the constructor mirrors the `if may_manage else None` conditional for both (cast threshold with `float(...)` — `Numeric` arrives as `Decimal`).

- [ ] **Step 4: Run the suites**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_tenant_retry_config.py tests/integration/control_plane/test_platform_tenant_observer.py -q
```

Expected: all PASS (observer suite proves no regression in the shared surface).

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend/
git commit -m "feat(platform): per-tenant retry-config endpoint with definer write + audit"
```

---

### Task 4: Frontend — platform API wrapper + PlatformSettings controls

**Files:**
- Modify: `vera-frontend/src/lib/api/platform.ts`
- Modify: `vera-frontend/src/pages/PlatformSettings.tsx`
- Test: `vera-frontend/src/lib/api/platform.test.ts` (extend if present, else create following the house vi.mock pattern)
- Test: `vera-frontend/src/pages/PlatformSettings.test.tsx` (create; jsdom/RTL infra exists)

**Interfaces:**
- Consumes: Task 3's endpoint and the widened `TenantSummary`.
- Produces: platform UI controls; no downstream consumers.

- [ ] **Step 1: API wrapper (failing test first)**

Extend `TenantSummary` in `platform.ts` with `auto_retry_enabled: boolean | null` and `retry_fill_threshold: number | null` (same tri-state comment as `observer_enabled`), then below `setTenantObserverEnabled`:

```typescript
export type TenantRetryConfig = {
  tenant_id: string
  auto_retry_enabled: boolean
  retry_fill_threshold: number
}

/** Set a tenant's auto-retry flag and/or fill threshold (0–1). Requires
 *  `platform:tenants:manage`. Omitted fields stay unchanged. */
export function setTenantRetryConfig(
  tenantId: string,
  patch: { auto_retry_enabled?: boolean; retry_fill_threshold?: number },
) {
  return apiRequest<TenantRetryConfig>(
    `/platform/tenants/${encodeURIComponent(tenantId)}/retry-config`,
    {
      method: "POST",
      body: patch,
      headers: { "Idempotency-Key": randomId() },
    },
  )
}
```

Test asserts path, method, body-passthrough, and that an Idempotency-Key header is present — copy the mocking style of the sibling api tests (check `platform.test.ts` first; if absent, mirror `tenantConfig.test.ts`).

- [ ] **Step 2: PlatformSettings controls (failing component test first)**

Component test (`PlatformSettings.test.tsx`, RTL): mock `@/lib/api/platform` and `@/lib/auth/permissions` (`usePermission` → true); `listTenants` resolves one tenant `{observer_enabled: true, auto_retry_enabled: false, retry_fill_threshold: 0.5}`. Cases: renders the auto-retry switch unchecked and threshold input showing `50`; toggling the switch calls `setTenantRetryConfig(id, {auto_retry_enabled: true})`; changing threshold to `40` and blurring calls `setTenantRetryConfig(id, {retry_fill_threshold: 0.4})`; a rejected call reverts the optimistic switch and shows the error.

Implementation in `PlatformSettings.tsx`:
- Two new table columns: "Auto retry" (a `Switch`, exact copy of the observer `onToggle` optimistic pattern but calling `setTenantRetryConfig(tenant.id, {auto_retry_enabled: next})` and reverting `auto_retry_enabled` on failure) and "Retry threshold %" (an `<Input type="number" min={0} max={100}>` rendering `Math.round((t.retry_fill_threshold ?? 0) * 100)`, local draft state per row, committed on blur/Enter via `setTenantRetryConfig(tenant.id, {retry_fill_threshold: draft / 100})`, row state updated from the response — no optimistic write needed for the input, just a per-row saving flag).
- Page copy: extend the header paragraph with one sentence: auto-retry redials a form whose fill % is below the threshold after a bot-ended call.
- Keep the `?? false` tri-state note convention for the new switch.

- [ ] **Step 3: Run FE tests**

```bash
cd vera-frontend && npx tsc -b && npx eslint . && npm test -- Platform
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/lib/api/platform.ts vera-frontend/src/lib/api/platform.test.ts vera-frontend/src/pages/PlatformSettings.tsx vera-frontend/src/pages/PlatformSettings.test.tsx
git commit -m "feat(fe): platform per-tenant auto-retry toggle and threshold"
```

---

### Task 5: Full gates + simplify pass

**Files:** none new.

- [ ] **Step 1:** `cd vera-backend && just check` — all green.
- [ ] **Step 2:** `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build` — all green.
- [ ] **Step 3:** Boot check (consumer inputs changed): `just api` in the background with `LOCAL_KMS_MASTER_KEY` + `VERA_LIVEKIT_URL=ws://localhost:7880`, idle 2+ sweep intervals, zero dispatcher/sweeper/consumer errors in the log, kill cleanly.
- [ ] **Step 4:** Run the repo-mandated simplify pass over the branch diff; apply safe refinements only within the branch's files; say explicitly if nothing changed.
- [ ] **Step 5:** Re-run BOTH gates if anything changed. Commit `refactor: simplify pass over tenant auto-retry config` only if there are changes.

---

## Acceptance traceability

| Requirement | Where satisfied |
|---|---|
| Threshold reduced to 50% | Task 1 (default 0.50 + backfill of untouched 0.95 rows) |
| Platform feature flag for auto-retry | Task 1 (column) + Task 2 (AND gate) + Task 3 (endpoint) + Task 4 (UI switch) |
| Threshold configurable per tenant from platform | Task 3 (endpoint field) + Task 4 (UI input) |
| Follows Abdullah's tenant-toggle pattern | Definer fn + platform.py + PlatformSettings mirroring PR #11 throughout |
| Env var behavior | Retained as deployment kill-switch, ANDed (Task 2) |
