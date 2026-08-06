# Analytics Dashboard (VR2-44 + VR2-45) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the placeholder Analytics tab into a live provider panel (VR2-44) + tenant history report (VR2-45), add the queue-limit card to Live Monitoring, behind a new `reports:dashboard` permission.

**Architecture:** One new backend router (`api/v1/analytics.py`) with four count/average-only endpoints computed straight from the raw rows at request time (no precomputed summaries). The live panel reuses Live Monitoring's exact counting primitives — `ACTIVE_CALL_STATUSES` (today's private `_ACTIVE_STATUSES` in `calls.py`, made public) and `call_authz.visible_to` — so the two screens can never disagree. The queue-limit card mirrors the dispatcher's slot math (`DISPATCH_ACTIVE_FORM_STATUSES` vs `Tenant.max_concurrent_calls`), tenant-wide on purpose. Completion % is frozen onto the `Call` row at terminal status so history never changes after the fact. Frontend: new `Analytics` page (8-second polling, same conventions as Live Monitoring) + a `QueueLimitCard` on Live Monitoring + a history report section with metric cards and recharts charts.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + pytest (backend, mypy --strict, ruff); React 19 + Vite + TS + vitest + recharts (new dependency) (frontend).

**Plan doc (product spec):** `docs/superpowers/plans/2026-08-03-vr2-44-45-reporting-dashboard-plan.txt`. This plan implements its Steps 1 and 2. Step 3 (recording IVR outcome / cost / latency in the agent worker) is deliberately out of scope — it ships as separate PRs with its own plan.

## Global Constraints

- Work on branch `feat/reporting-dashboard` (current branch).
- Backend gate: `just check` (ruff check + ruff format --check + mypy --strict + pytest) — run verbatim, never a subset. Integration tests need `just up` + `just migrate`. Backend cwd: `vera-backend/`.
- Frontend gate: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` — all four, on the exact final tree. Frontend cwd: `vera-frontend/`.
- **Commits: never add a `Co-Authored-By: Claude` trailer** (team git rule, overrides any default).
- Python: PEP 695 generics only; `asyncio` only (never `anyio`); type hints everywhere.
- Timestamps: DB clock (`func.now()`), never Python `datetime.now()` — in seeds/tests too, except where a test pins an explicit datetime on purpose.
- Endpoints: `ok(...)` / `ResponseModel[T]`; errors via `CustomAPIException` subclasses, never `HTTPException`; `Cache-Control: no-store` on every endpoint here.
- **Every analytics response is counts/averages/catalog-names only — no patient field, ever.** That is what exempts these endpoints from `emit_phi_read_audit` (precedent: `calls.py::call_stats`). Adding any patient identifier or `health_reason` makes it a PHI disclosure — don't.
- Migrations: alembic-random revision ids via `just makemigration` / `uv run alembic revision`; every DDL idempotent (`IF NOT EXISTS`); seed migrations' `downgrade()` raises `RuntimeError`; statements exposed as module-level `UPGRADE_STATEMENTS: tuple[str, ...]`.
- Permission string, verbatim everywhere: `reports:dashboard`.
- No-provider bucket label, verbatim: `(No provider)` (frontend display; the API carries `provider_name: null`).
- Poll interval: `POLL_MS = 8000` (same as Live Monitoring).
- Frontend: API types snake_case mirroring the backend; `import type` for type-only imports (`verbatimModuleSyntax`); no PHI in URLs / storage / console; after touching `package.json`, verify with `npm ci` (pinned npm via Corepack).
- Comments: only non-obvious constraints, one line; docstrings one sentence.

## File Structure

| File | Responsibility |
|---|---|
| `vera-backend/packages/vera_core/src/vera_core/models/rbac_defaults.py` | Modify — add `reports:dashboard` to the catalog + SUPERVISOR grant. |
| `vera-backend/migrations/versions/<gen>_seed_reports_dashboard_permission.py` | **New** — seed permission + system-role grants. |
| `vera-backend/packages/vera_core/src/vera_core/services/call_lifecycle.py` | Modify — freeze `form.completion_pct` onto the call at terminal status. |
| `vera-backend/migrations/versions/<gen>_backfill_call_completion_pct.py` | **New** — one-time backfill for already-terminal calls. |
| `vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py` | Modify — publicize `DISPATCH_ACTIVE_FORM_STATUSES`. |
| `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py` | Modify — publicize `ACTIVE_CALL_STATUSES`. |
| `vera-backend/apps/control_plane/src/control_plane/api/v1/analytics.py` | **New** — the four endpoints + their response models. |
| `vera-backend/apps/control_plane/src/control_plane/api/v1/__init__.py` | Modify — register the router. |
| `vera-backend/tests/integration/control_plane/test_analytics.py` | **New** — permission, isolation, consistency, spot-check tests. |
| `vera-frontend/src/lib/api/analytics.ts` (+ `.test.ts`) | **New** — typed API module. |
| `vera-frontend/src/components/monitoring/QueueLimitCard.tsx` (+ test) | **New** — the VR2-44 card on Live Monitoring. |
| `vera-frontend/src/pages/LiveMonitoring.tsx` | Modify — fetch queue status in the existing poll, render the card. |
| `vera-frontend/src/pages/Analytics.tsx` (+ components in `src/components/analytics/`) | **New** — the Analytics page: live panel + history report. |
| `vera-frontend/src/lib/analytics/report.ts` (+ test) | **New** — pure helpers: date presets, deltas, duration formatting. |
| `vera-frontend/src/lib/nav.ts`, `src/App.tsx` | Modify — gate the tab on `reports:dashboard`, mount the page. |

---

### Task 1: `reports:dashboard` permission (catalog + seed migration)

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Test: `vera-backend/tests/unit/test_rbac_defaults.py` (append)
- Create: `vera-backend/migrations/versions/<generated>_seed_reports_dashboard_permission.py`

**Interfaces:**
- Produces: permission code `reports:dashboard` seeded into the catalog and granted to `SUPER_ADMIN`, `TENANT_ADMIN`, `SUPERVISOR` (not `VIRTUAL_ASSISTANT` — VAs keep Live Monitoring via `calls:read`; a tenant admin can grant more via role editing). The integration conftest seeds from `SYSTEM_ROLES`, so Tasks 3–5's tests rely on this grant. Consumed by `require("reports:dashboard")` in Tasks 4–5 and by the frontend nav gate in Task 8.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_rbac_defaults.py` (reuse its existing imports of `DEFAULT_PERMISSIONS` / `SYSTEM_ROLES`):

```python
def test_reports_dashboard_permission_seeded() -> None:
    assert "reports:dashboard" in DEFAULT_PERMISSIONS
    assert "reports:dashboard" in SYSTEM_ROLES["TENANT_ADMIN"]
    assert "reports:dashboard" in SYSTEM_ROLES["SUPERVISOR"]
    # VAs reach Live Monitoring via calls:read; the dashboard is an explicit grant.
    assert "reports:dashboard" not in SYSTEM_ROLES["VIRTUAL_ASSISTANT"]
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd vera-backend && uv run pytest tests/unit/test_rbac_defaults.py -q
```

Expected: FAIL — `'reports:dashboard' in DEFAULT_PERMISSIONS` is False.

- [ ] **Step 3: Add the permission to the catalog**

In `rbac_defaults.py`:

1. Add to `DEFAULT_PERMISSIONS` (after the `"audit:read"` entry, keeping the dict's grouping):

```python
    "reports:dashboard": "View the analytics dashboard (live panel and history report)",
```

2. Add `"reports:dashboard",` to the `SYSTEM_ROLES["SUPERVISOR"]` frozenset (alphabetically it fits after `"recordings:read"`; match the set's existing ordering style). `TENANT_ADMIN` is `frozenset(DEFAULT_PERMISSIONS)` and `SUPER_ADMIN` is `frozenset(ALL_PERMISSIONS)`, so both pick it up automatically. Leave `VIRTUAL_ASSISTANT` unchanged.

- [ ] **Step 4: Run to verify it passes**

```bash
cd vera-backend && uv run pytest tests/unit/test_rbac_defaults.py -q
```

Expected: PASS (the file's pre-existing catalog assertions must also still pass — if one enumerates the full catalog, extend it).

- [ ] **Step 5: Write the seed migration**

```bash
cd vera-backend && uv run alembic revision -m "seed reports dashboard permission"
```

Copy the exact shape of `migrations/versions/20260723_1520_e3e633747040_seed_llm_config_permissions.py` (imports, docstring style). Body:

```python
UPGRADE_STATEMENTS: tuple[str, ...] = (
    """
    INSERT INTO permission (id, code, description)
    VALUES (gen_random_uuid(), 'reports:dashboard',
            'View the analytics dashboard (live panel and history report)')
    ON CONFLICT (code) DO NOTHING
    """,
    """
    INSERT INTO role_permission (id, tenant_id, role_id, permission_id)
    SELECT gen_random_uuid(), NULL, r.id, p.id
    FROM role r, permission p
    WHERE r.tenant_id IS NULL
      AND r.name IN ('SUPER_ADMIN', 'TENANT_ADMIN', 'SUPERVISOR')
      AND p.code = 'reports:dashboard'
    ON CONFLICT (role_id, permission_id) DO NOTHING
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("seed migrations are not reversible")
```

- [ ] **Step 6: Run the migration and verify**

```bash
cd vera-backend && just migrate
docker compose exec -T postgres psql -U vera -d vera -c \
  "SELECT r.name FROM role r JOIN role_permission rp ON rp.role_id = r.id JOIN permission p ON p.id = rp.permission_id WHERE p.code = 'reports:dashboard' ORDER BY r.name;"
```

Expected: `SUPER_ADMIN`, `SUPERVISOR`, `TENANT_ADMIN`. Re-run `just migrate` — must be a no-op. (Adjust psql user/db to `docker-compose.yml` if they differ.)

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/rbac_defaults.py vera-backend/tests/unit/test_rbac_defaults.py vera-backend/migrations/versions/
git commit -m "feat(rbac): reports:dashboard permission for the analytics dashboard"
```

---

### Task 2: Freeze completion % onto the Call at terminal status

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/call_lifecycle.py:46`
- Test: `vera-backend/tests/unit/services/test_call_lifecycle.py` (append; if the file doesn't exist, create it with exactly the code below)
- Create: `vera-backend/migrations/versions/<generated>_backfill_call_completion_pct.py`

**Interfaces:**
- Consumes: `Call.completion_pct` (existing dead column, `Numeric(5,2)` NOT NULL default 0) and `PatientForm.completion_pct` (actively maintained).
- Produces: every call that reaches a terminal status carries the form's completion % as of that moment. Task 5's `avg_completion_pct` metric reads `Call.completion_pct` and relies on this.

Background: `call.completion_pct` has never been written; only `patient_form.completion_pct` is maintained, and the form keeps changing after the call (human edits, retry calls) — which would make historical reports drift. All three terminal writers (worker-event consumer + pipeline sweeper via `call_closeout.close_call`, and the dispatcher's dial-failure path) funnel through `apply_terminal_call_status`, so that function is the single choke point. The only bypass is `close_call`'s form-deleted branch (`call_closeout.py:102`) — no form exists there, so there is nothing to freeze and 0 is correct.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/services/test_call_lifecycle.py`, reusing its existing fakes/helpers if the file exists (adapt the two fakes below to them); otherwise create the file:

```python
from decimal import Decimal
from types import SimpleNamespace

from vera_core.models.enums import CallStatus
from vera_core.services.call_lifecycle import apply_terminal_call_status


def _call() -> SimpleNamespace:
    return SimpleNamespace(current_status="active", completion_pct=Decimal("0"))


def _form(status: str = "in_call") -> SimpleNamespace:
    return SimpleNamespace(status=status, completion_pct=Decimal("62.50"), retry_count=0)


def test_terminal_status_freezes_form_completion_onto_the_call() -> None:
    call, form = _call(), _form()
    apply_terminal_call_status(call, form, CallStatus.COMPLETED, tenant_max_retries=3)
    assert call.completion_pct == Decimal("62.50")


def test_freeze_happens_even_when_the_form_edge_is_illegal() -> None:
    """The form edge is best-effort by design; the call's snapshot must not be."""
    call = _call()
    form = _form(status="completed")  # cannot leave COMPLETED → InvalidTransitionError path
    apply_terminal_call_status(call, form, CallStatus.COMPLETED, tenant_max_retries=3)
    assert call.current_status == "completed"
    assert call.completion_pct == Decimal("62.50")
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd vera-backend && uv run pytest tests/unit/services/test_call_lifecycle.py -q
```

Expected: FAIL — `call.completion_pct` is still `Decimal("0")`.

- [ ] **Step 3: Implement the freeze**

In `call_lifecycle.py::apply_terminal_call_status`, directly after `call.current_status = status.value`:

```python
    call.current_status = status.value
    # Freeze the form's completion as of THIS call's end: the form keeps evolving
    # (human edits, retry calls), but a historical report must never change.
    call.completion_pct = form.completion_pct
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd vera-backend && uv run pytest tests/unit/services/test_call_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 5: Write the backfill migration**

```bash
cd vera-backend && uv run alembic revision -m "backfill call completion pct"
```

Body (same import scaffolding as Task 1's migration):

```python
UPGRADE_STATEMENTS: tuple[str, ...] = (
    # One-time catch-up: calls closed before the freeze existed still read the
    # column default (0; nothing ever wrote it). Copy the form's CURRENT value —
    # the number the UI showed for those calls until now — so history isn't a
    # wall of zeros. Idempotent: already-copied rows no longer match `= 0`.
    """
    UPDATE call SET completion_pct = pf.completion_pct
    FROM patient_form pf
    WHERE pf.id = call.form_id
      AND call.current_status IN ('completed', 'failed', 'no_answer', 'busy', 'canceled')
      AND call.completion_pct = 0
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Data backfill — nothing to reverse (the previous state was "never written").
    pass
```

- [ ] **Step 6: Run the migration and the touched suites**

```bash
cd vera-backend && just migrate
uv run pytest tests/unit/services/ tests/integration/control_plane/test_calls.py -q
```

Expected: migration applies cleanly twice (idempotent); suites PASS (the closeout integration tests exercise `apply_terminal_call_status` with real ORM rows).

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/services/call_lifecycle.py vera-backend/tests/unit/services/test_call_lifecycle.py vera-backend/migrations/versions/
git commit -m "feat(calls): freeze form completion pct onto the call at terminal status"
```

---

### Task 3: Analytics router + `GET /analytics/queue-status`

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py` (~line 89)
- Create: `vera-backend/apps/control_plane/src/control_plane/api/v1/analytics.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/__init__.py`
- Test: `vera-backend/tests/integration/control_plane/test_analytics.py` (create)

**Interfaces:**
- Consumes: `Tenant.max_concurrent_calls`; the dispatcher's active-form statuses.
- Produces: `DISPATCH_ACTIVE_FORM_STATUSES` (public, in `queue_dispatcher`); `GET /api/v1/analytics/queue-status` → `{"limit": int, "active": int, "in_queue": int}` in the standard `ok()` envelope, gated `calls:read` — consumed by Task 6's `getQueueStatus()` and Task 7's card.

- [ ] **Step 1: Publicize the dispatcher's active-form set**

In `queue_dispatcher.py`, rename `_ACTIVE_FORM_STATUSES` → `DISPATCH_ACTIVE_FORM_STATUSES` (it becomes a cross-module contract: the queue-status card must mirror the dispatcher's slot math exactly, so it must read the same constant, not a copy). Update every reference:

```bash
cd vera-backend && grep -rn "_ACTIVE_FORM_STATUSES" packages apps tests
```

Rename all hits (definition, the slot-math count, and any test imports). Keep the existing comment above it, adding that the analytics queue-status endpoint reads it too.

- [ ] **Step 2: Write the failing integration tests**

Create `tests/integration/control_plane/test_analytics.py`. Reuse the conftest fixtures (`client`, `rbac_world`, `admin_sessionmaker`) and copy the `_auth` helper from `test_calls.py`. Seed forms by copying the form-seeding helper used in `tests/integration/control_plane/test_call_queue.py` (`_seed_ready_form` or its enclosing fixture) — do not invent a new seeding path; `PatientForm.schema_version_id` is NOT NULL and that helper already satisfies it.

```python
"""Integration tests for /api/v1/analytics — counts only, real RLS + RBAC."""

QUEUE_STATUS_PATH = "/api/v1/analytics/queue-status"


async def test_queue_status_mirrors_dispatcher_math(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Seed one form per dispatcher-relevant status; the card must count forms in
    the dispatcher's active set as 'active', in_queue forms as 'in_queue', and
    ready_for_processing in neither — exact before/after deltas prove all three."""
    headers = _auth(rbac_world.virtual_assistant_token)
    before = (await client.get(QUEUE_STATUS_PATH, headers=headers)).json()["data"]

    async with admin_sessionmaker() as session, session.begin():
        for status in ("in_queue", "in_call", "ai_processing", "ready_for_processing"):
            form_id = await _seed_form(session, rbac_world.tenant_id)  # see Step 2 intro note
            await session.execute(
                update(PatientForm).where(PatientForm.id == form_id).values(status=status)
            )
        limit = (
            await session.execute(
                select(Tenant.max_concurrent_calls).where(Tenant.id == rbac_world.tenant_id)
            )
        ).scalar_one()

    resp = await client.get(QUEUE_STATUS_PATH, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()["data"]
    assert data["limit"] == limit
    assert data["active"] == before["active"] + 2    # in_call + ai_processing only
    assert data["in_queue"] == before["in_queue"] + 1  # ready_for_processing counts nowhere


async def test_queue_status_denied_without_calls_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403


async def test_queue_status_is_tenant_isolated(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A queued form in ANOTHER tenant never shows in this tenant's counts."""
    before = (
        await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.admin_token))
    ).json()["data"]
    async with admin_sessionmaker() as session, session.begin():
        other_form = await _seed_form(session, rbac_world.other_tenant_id)
        await session.execute(
            update(PatientForm).where(PatientForm.id == other_form).values(status="in_queue")
        )
    after = (
        await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.admin_token))
    ).json()["data"]
    assert after == before
```

`_seed_form(session, tenant_id)` above stands for the copied seeding helper — give it whatever real name/signature the copied code has, and use greater-equal assertions (`>=`) where shown because the shared `rbac_world` may carry forms from sibling tests. Use exact-delta assertions (before/after) where the test seeds its own rows, as in the isolation test.

- [ ] **Step 3: Run to verify they fail**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_analytics.py -q
```

Expected: FAIL with 404s — the route doesn't exist. (Needs `just up` + `just migrate` first.)

- [ ] **Step 4: Create the router with the endpoint**

Create `apps/control_plane/src/control_plane/api/v1/analytics.py`. Copy the import block style from `api/v1/calls.py` (same `require` / `VerifiedIdentity` / `TenantId` / `TenantSession` / `ResponseModel` / `ok` / `CustomAPIResponse` / `DefaultExceptionCode` import paths that file uses):

```python
"""Tenant analytics: live queue status, the live provider panel, the history report.

Counts, averages, and catalog names only — no patient field ever leaves this module,
which is what exempts it from the PHI display-path audit (precedent: calls.py::call_stats).
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from vera_core.models import PatientForm, Tenant
from vera_core.models.enums import FormStatus
from vera_core.services.queue_dispatcher import DISPATCH_ACTIVE_FORM_STATUSES

router = APIRouter(tags=["analytics"])


class QueueStatus(BaseModel):
    """Tenant-wide mirror of the dispatcher's slot math (queue_dispatcher.try_dispatch)."""

    limit: int
    active: int
    in_queue: int


@router.get(
    "/analytics/queue-status",
    response_model=ResponseModel[QueueStatus],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def queue_status(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[QueueStatus]:
    """Why a queued call hasn't dialed yet. Tenant-wide (not per-user) on purpose:
    the dial ceiling is shared, so another VA's active calls DO hold your form."""
    response.headers["Cache-Control"] = "no-store"
    limit = (
        await session.execute(
            select(Tenant.max_concurrent_calls).where(Tenant.id == tenant_id)
        )
    ).scalar_one()
    active, in_queue = (
        await session.execute(
            select(
                func.count().filter(
                    PatientForm.status.in_([s.value for s in DISPATCH_ACTIVE_FORM_STATUSES])
                ),
                func.count().filter(PatientForm.status == FormStatus.IN_QUEUE.value),
            ).select_from(PatientForm)
        )
    ).one()
    return ok(QueueStatus(limit=limit, active=active, in_queue=in_queue))
```

If `DISPATCH_ACTIVE_FORM_STATUSES` already holds `str` values (check the definition after Step 1's rename), drop the `.value` comprehension and pass `list(...)` directly.

- [ ] **Step 5: Register the router**

In `api/v1/__init__.py`, mirroring the existing lines exactly (alphabetical placement):

```python
from control_plane.api.v1.analytics import router as analytics_router
...
router.include_router(analytics_router)
```

- [ ] **Step 6: Run to verify they pass**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_analytics.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py vera-backend/apps/control_plane/src/control_plane/api/v1/ vera-backend/tests/
git commit -m "feat(analytics): queue-status endpoint mirroring the dispatcher slot math"
```

---

### Task 4: `GET /analytics/live` — the provider panel

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py:107-114`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/analytics.py`
- Test: `vera-backend/tests/integration/control_plane/test_analytics.py` (append)

**Interfaces:**
- Consumes: `visible_to` from `control_plane.call_authz`; `Call`, `InsuranceProvider`, `PatientForm` models; `ProviderStatus` enum.
- Produces: `ACTIVE_CALL_STATUSES` (public, in `calls.py`); `GET /api/v1/analytics/live` → `{"rows": [{"provider_id": UUID|null, "provider_name": str|null, "in_queue": int, "active": int}]}`, gated `reports:dashboard` — consumed by Task 6's `getLivePanel()`.

The consistency contract (the ticket's hard rule): "active" per provider uses the **same status set and the same per-user visibility** as `GET /calls` / `GET /calls/stats`, so the panel's total always equals Live Monitoring's list length. "In queue" counts `in_queue` forms tenant-wide (forms have no per-user visibility anywhere in the app) grouped by resolving their free-text provider against the catalog the same way the dispatcher does (case-insensitive, trimmed, ACTIVE providers only); unresolved goes to the `provider_id: null` bucket so totals always add up.

- [ ] **Step 1: Publicize the active-call status set**

In `calls.py`, rename `_ACTIVE_STATUSES` → `ACTIVE_CALL_STATUSES` and update every reference:

```bash
cd vera-backend && grep -rn "_ACTIVE_STATUSES" apps tests
```

(Definition at `calls.py:107`, uses in `list_calls` and `call_stats`, plus any test imports.) Extend its comment: this tuple is the single definition of a live call, shared by Live Monitoring and the analytics live panel — the ticket requires the two screens to agree, so never fork it.

- [ ] **Step 2: Write the failing integration tests**

Append to `test_analytics.py`. Reuse the conftest `seed_call(sessionmaker, tenant_id, form_id, *, initiated_by_id=None, status="initiated", published=False)` helper (`conftest.py:577`) and the form-seeding helper from Task 3:

```python
LIVE_PATH = "/api/v1/analytics/live"


async def test_live_panel_matches_live_monitoring(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The ticket's hard rule: sum(active) == /calls/stats live == len(GET /calls)."""
    headers = _auth(rbac_world.supervisor_token)
    # Seed for the SUPERVISOR persona: one own active call, one published call
    # owned by someone else (visible), one unpublished call owned by someone
    # else (INVISIBLE), one queued form.
    async with admin_sessionmaker() as session, session.begin():
        form_a = await _seed_form(session, rbac_world.tenant_id)
        form_b = await _seed_form(session, rbac_world.tenant_id)
        form_c = await _seed_form(session, rbac_world.tenant_id)
        form_q = await _seed_form(session, rbac_world.tenant_id)
        await seed_call(session, rbac_world.tenant_id, form_a,
                        initiated_by_id=rbac_world.supervisor_id, status="active")
        await seed_call(session, rbac_world.tenant_id, form_b,
                        initiated_by_id=rbac_world.admin_id, status="active", published=True)
        await seed_call(session, rbac_world.tenant_id, form_c,
                        initiated_by_id=rbac_world.admin_id, status="active")  # hidden
        await session.execute(
            update(PatientForm).where(PatientForm.id == form_q).values(status="in_queue")
        )

    panel = (await client.get(LIVE_PATH, headers=headers)).json()["data"]
    stats = (await client.get("/api/v1/calls/stats", headers=headers)).json()["data"]
    live_list = (await client.get("/api/v1/calls", headers=headers)).json()["data"]

    assert sum(r["active"] for r in panel["rows"]) == stats["live"] == len(live_list)
    assert sum(r["in_queue"] for r in panel["rows"]) >= 1


async def test_unmatched_provider_text_lands_in_the_null_bucket(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        form_id = await _seed_form(session, rbac_world.tenant_id)
        await session.execute(
            update(PatientForm)
            .where(PatientForm.id == form_id)
            .values(status="in_queue", insurance_provider="No Such Payer Inc")
        )

    rows = (
        (await client.get(LIVE_PATH, headers=_auth(rbac_world.admin_token))).json()["data"]["rows"]
    )
    bucket = next(r for r in rows if r["provider_id"] is None)
    assert bucket["provider_name"] is None
    assert bucket["in_queue"] >= 1


async def test_live_panel_requires_reports_dashboard(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    # VIRTUAL_ASSISTANT holds calls:read but NOT reports:dashboard.
    resp = await client.get(LIVE_PATH, headers=_auth(rbac_world.virtual_assistant_token))
    assert resp.status_code == 403
    ok_resp = await client.get(LIVE_PATH, headers=_auth(rbac_world.supervisor_token))
    assert ok_resp.status_code == 200
```

If `seed_call` doesn't accept a session (check its signature — it may take a sessionmaker), adapt the calls accordingly.

- [ ] **Step 3: Run to verify they fail**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_analytics.py -q
```

Expected: new tests FAIL with 404 (route missing); Task 3's tests still PASS.

- [ ] **Step 4: Implement the endpoint**

Append to `analytics.py` (extend imports: `and_` from sqlalchemy, `UUID` from uuid, `defaultdict` from collections, `Call`, `InsuranceProvider` from `vera_core.models`, `ProviderStatus` from `vera_core.models.enums`, `visible_to` from `control_plane.call_authz`, `ACTIVE_CALL_STATUSES` from `control_plane.api.v1.calls`):

```python
class LiveProviderRow(BaseModel):
    provider_id: UUID | None
    provider_name: str | None  # None ⇒ the frontend's "(No provider)" bucket
    in_queue: int
    active: int


class LivePanel(BaseModel):
    rows: list[LiveProviderRow]


@router.get(
    "/analytics/live",
    response_model=ResponseModel[LivePanel],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def live_panel(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[LivePanel]:
    """Live counts per provider. Active uses Live Monitoring's exact status set and
    per-user visibility (the ticket requires the two screens to agree); queued forms
    resolve their free-text provider the same way the dispatcher does."""
    response.headers["Cache-Control"] = "no-store"
    active_rows = (
        await session.execute(
            select(Call.insurance_provider_id, func.count())
            .where(
                Call.current_status.in_(list(ACTIVE_CALL_STATUSES)),
                visible_to(caller.user_id),
            )
            .group_by(Call.insurance_provider_id)
        )
    ).all()
    queued_rows = (
        await session.execute(
            select(InsuranceProvider.id, func.count())
            .select_from(PatientForm)
            .outerjoin(
                InsuranceProvider,
                and_(
                    # Same resolve as queue_dispatcher._resolve_provider: trimmed,
                    # case-insensitive, ACTIVE catalog entries only.
                    func.lower(InsuranceProvider.name)
                    == func.lower(func.trim(PatientForm.insurance_provider)),
                    InsuranceProvider.status == ProviderStatus.ACTIVE.value,
                ),
            )
            .where(PatientForm.status == FormStatus.IN_QUEUE.value)
            .group_by(InsuranceProvider.id)
        )
    ).all()
    counts: dict[UUID | None, dict[str, int]] = defaultdict(lambda: {"in_queue": 0, "active": 0})
    for provider_id, n in active_rows:
        counts[provider_id]["active"] = n
    for provider_id, n in queued_rows:
        counts[provider_id]["in_queue"] = n
    named = [pid for pid in counts if pid is not None]
    names: dict[UUID, str] = (
        dict(
            (
                await session.execute(
                    select(InsuranceProvider.id, InsuranceProvider.name).where(
                        InsuranceProvider.id.in_(named)
                    )
                )
            ).all()
        )
        if named
        else {}
    )
    rows = sorted(
        (
            LiveProviderRow(
                provider_id=pid,
                provider_name=names.get(pid) if pid is not None else None,
                in_queue=c["in_queue"],
                active=c["active"],
            )
            for pid, c in counts.items()
        ),
        # Named providers alphabetically; the no-provider bucket last.
        key=lambda r: (r.provider_name is None, r.provider_name or ""),
    )
    return ok(LivePanel(rows=rows))
```

- [ ] **Step 5: Run to verify they pass**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_analytics.py tests/integration/control_plane/test_calls.py -q
```

Expected: PASS (including `test_calls.py`, which exercises the renamed constant).

- [ ] **Step 6: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/ vera-backend/tests/
git commit -m "feat(analytics): live provider panel sharing Live Monitoring's counting rules"
```

---

### Task 5: `GET /analytics/report` + `GET /analytics/filters` — the history report

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/analytics.py`
- Test: `vera-backend/tests/integration/control_plane/test_analytics.py` (append)

**Interfaces:**
- Consumes: `Call` (incl. `completion_pct` from Task 2), `InterventionEvent`, `AppUser`, `InsuranceProvider`; `TERMINAL_CALL_STATUSES` from `vera_core.models.call`.
- Produces (consumed by Task 6's `getHistoryReport()` / `getReportFilters()`):
  - `GET /api/v1/analytics/report?date_from&date_to&provider_id&va_id` → `{"current": Metrics, "previous": Metrics, "calls_per_day": [{"day", "calls"}], "interventions_by_type": [{"type", "count"}]}` where `Metrics = {"call_volume": int, "avg_duration_seconds": float|null, "avg_completion_pct": float|null, "intervened_calls": int, "intervention_rate": float|null}`.
  - `GET /api/v1/analytics/filters` → `{"providers": [{"id","name"}], "vas": [{"id","name"}]}`.

Metric definitions (from the plan doc, pinned here):
- **call_volume** — calls created in `[date_from, date_to)`.
- **avg_duration_seconds** — `avg(ended_at - started_at)` over calls in the window that have both stamps; `null` when none.
- **avg_completion_pct** — `avg(call.completion_pct)` over calls in the window that reached a terminal status (that's when the value is frozen); `null` when none.
- **intervention_rate** — `intervened_calls / call_volume`; a call is intervened when it has ≥1 `intervention_event` row (permanent, never erased); `null` when volume is 0.
- **previous** — identical metrics over the equal-length window ending at `date_from`.
- Days bucket in UTC (same convention as `call_stats`'s "today").

- [ ] **Step 1: Write the failing spot-check test**

Append to `test_analytics.py`. This is the plan doc's accuracy promise as a test: seed a known world, compute expectations by hand, assert exact equality. Use a **fixed** window in the far past so sibling tests' rows (created "now") can never leak in, and pin `created_at` explicitly:

```python
from datetime import UTC, datetime

REPORT_PATH = "/api/v1/analytics/report"
FILTERS_PATH = "/api/v1/analytics/filters"

_FROM = datetime(2026, 1, 8, tzinfo=UTC)
_TO = datetime(2026, 1, 15, tzinfo=UTC)
_PREV = datetime(2026, 1, 3, tzinfo=UTC)  # inside the previous 7-day window


async def _seed_report_world(
    session: AsyncSession, world: RBACWorld
) -> None:
    """4 calls in the window (2 completed w/ known durations+completion, 1 active,
    1 canceled), 1 whisper intervention on one of them, 1 call in the previous window."""
    specs = [
        # (status, started offset min, ended offset min, completion)
        ("completed", 0, 5, "80.00"),
        ("completed", 0, 10, "60.00"),
        ("active", 0, None, "0"),
        ("canceled", 0, 3, "20.00"),
    ]
    call_ids: list[UUID] = []
    for status, _start, end_min, completion in specs:
        form_id = await _seed_form(session, world.tenant_id)
        call_id = await seed_call(
            session, world.tenant_id, form_id,
            initiated_by_id=world.supervisor_id, status=status,
        )
        call_ids.append(call_id)
        started = _FROM.replace(hour=12)
        values: dict[str, object] = {
            "created_at": started,
            "started_at": started,
            "completion_pct": Decimal(completion),
        }
        if end_min is not None:
            values["ended_at"] = started + timedelta(minutes=end_min)
        await session.execute(update(Call).where(Call.id == call_id).values(**values))
    session.add(
        InterventionEvent(
            tenant_id=world.tenant_id,
            call_id=call_ids[0],
            supervisor_id=world.supervisor_id,
            type="whisper",
        )
    )
    # One call in the PREVIOUS window.
    prev_form = await _seed_form(session, world.tenant_id)
    prev_call = await seed_call(
        session, world.tenant_id, prev_form,
        initiated_by_id=world.supervisor_id, status="completed",
    )
    await session.execute(
        update(Call).where(Call.id == prev_call).values(created_at=_PREV)
    )


async def test_report_matches_hand_computed_numbers(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_report_world(session, rbac_world)

    resp = await client.get(
        REPORT_PATH,
        params={"date_from": _FROM.isoformat(), "date_to": _TO.isoformat()},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    cur = data["current"]
    assert cur["call_volume"] == 4
    # Durations: 300s, 600s, 3*60=180s over the three ended calls → avg 360s.
    assert cur["avg_duration_seconds"] == pytest.approx(360.0)
    # Completion over TERMINAL calls only: (80 + 60 + 20) / 3.
    assert cur["avg_completion_pct"] == pytest.approx(160 / 3)
    assert cur["intervened_calls"] == 1
    assert cur["intervention_rate"] == pytest.approx(0.25)

    assert data["previous"]["call_volume"] == 1

    days = {row["day"]: row["calls"] for row in data["calls_per_day"]}
    assert days == {"2026-01-08": 4}
    assert data["interventions_by_type"] == [{"type": "whisper", "count": 1}]


async def test_report_filters_narrow_by_va(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """Reuses the world seeded above (session-scoped rbac_world): filtering by a VA
    who initiated nothing in the window returns zero volume."""
    resp = await client.get(
        REPORT_PATH,
        params={
            "date_from": _FROM.isoformat(),
            "date_to": _TO.isoformat(),
            "va_id": str(rbac_world.virtual_assistant_id),
        },
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.json()["data"]["current"]["call_volume"] == 0


async def test_report_rejects_inverted_range(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        REPORT_PATH,
        params={"date_from": _TO.isoformat(), "date_to": _FROM.isoformat()},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422


async def test_report_requires_reports_dashboard(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        REPORT_PATH,
        params={"date_from": _FROM.isoformat(), "date_to": _TO.isoformat()},
        headers=_auth(rbac_world.virtual_assistant_token),
    )
    assert resp.status_code == 403


async def test_filters_lists_active_providers_and_call_owners(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(FILTERS_PATH, headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {"id", "name"} == set(data["providers"][0]) if data["providers"] else True
    # The supervisor initiated calls above, so they appear as a VA filter option.
    assert any(v["id"] == str(rbac_world.supervisor_id) for v in data["vas"])
```

Adjust `seed_call`'s call/return shape to the real helper (it may return the call id or the row; it may take the sessionmaker). If a test ordering issue makes the shared-world reuse in `test_report_filters_narrow_by_va` fragile, seed its own rows instead — exactness beats cleverness.

- [ ] **Step 2: Run to verify they fail**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_analytics.py -q
```

Expected: new tests FAIL with 404.

- [ ] **Step 3: Implement the two endpoints**

Append to `analytics.py` (extend imports: `date`, `datetime`, `timedelta` from datetime; `Query` from fastapi; `AppUser`, `InterventionEvent` from `vera_core.models`; `TERMINAL_CALL_STATUSES` from `vera_core.models.call`; `CustomAPIException` per the import style of `calls.py`; `Annotated` from typing; `ColumnElement` from sqlalchemy if needed for typing):

```python
_TERMINAL_VALUES = [s.value for s in TERMINAL_CALL_STATUSES]
_MAX_RANGE = timedelta(days=366)


class ReportMetrics(BaseModel):
    call_volume: int
    avg_duration_seconds: float | None
    avg_completion_pct: float | None
    intervened_calls: int
    intervention_rate: float | None  # 0..1


class DayCount(BaseModel):
    day: date
    calls: int


class InterventionTypeCount(BaseModel):
    type: str
    count: int


class HistoryReport(BaseModel):
    current: ReportMetrics
    previous: ReportMetrics
    calls_per_day: list[DayCount]
    interventions_by_type: list[InterventionTypeCount]


class FilterOption(BaseModel):
    id: UUID
    name: str


class ReportFilterOptions(BaseModel):
    providers: list[FilterOption]
    vas: list[FilterOption]


def _call_window(
    date_from: datetime,
    date_to: datetime,
    provider_id: UUID | None,
    va_id: UUID | None,
) -> list[Any]:
    conds: list[Any] = [Call.created_at >= date_from, Call.created_at < date_to]
    if provider_id is not None:
        conds.append(Call.insurance_provider_id == provider_id)
    if va_id is not None:
        conds.append(Call.initiated_by_id == va_id)
    return conds


async def _window_metrics(session: AsyncSession, conds: list[Any]) -> ReportMetrics:
    volume, avg_duration, avg_completion = (
        await session.execute(
            select(
                func.count(),
                func.avg(func.extract("epoch", Call.ended_at - Call.started_at)).filter(
                    Call.started_at.is_not(None), Call.ended_at.is_not(None)
                ),
                # Completion is frozen onto the call at terminal status (call_lifecycle);
                # live calls still read the 0 default, so average terminals only.
                func.avg(Call.completion_pct).filter(
                    Call.current_status.in_(_TERMINAL_VALUES)
                ),
            )
            .select_from(Call)
            .where(*conds)
        )
    ).one()
    intervened = (
        await session.execute(
            select(func.count(func.distinct(InterventionEvent.call_id)))
            .select_from(InterventionEvent)
            .join(Call, Call.id == InterventionEvent.call_id)
            .where(*conds)
        )
    ).scalar_one()
    return ReportMetrics(
        call_volume=volume,
        avg_duration_seconds=float(avg_duration) if avg_duration is not None else None,
        avg_completion_pct=float(avg_completion) if avg_completion is not None else None,
        intervened_calls=intervened,
        intervention_rate=(intervened / volume) if volume else None,
    )


@router.get(
    "/analytics/report",
    response_model=ResponseModel[HistoryReport],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def history_report(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    date_from: datetime,
    date_to: datetime,
    provider_id: UUID | None = None,
    va_id: UUID | None = None,
    _caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[HistoryReport]:
    """Tenant-wide historical metrics, computed from the raw rows at request time so a
    manual spot-check over the same range always matches."""
    response.headers["Cache-Control"] = "no-store"
    if date_to <= date_from:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR, message="date_to must be after date_from"
        )
    if date_to - date_from > _MAX_RANGE:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR, message="date range is capped at 366 days"
        )
    current_conds = _call_window(date_from, date_to, provider_id, va_id)
    prev_from = date_from - (date_to - date_from)
    previous_conds = _call_window(prev_from, date_from, provider_id, va_id)

    current = await _window_metrics(session, current_conds)
    previous = await _window_metrics(session, previous_conds)

    # UTC day buckets — same convention as calls.py::call_stats "today".
    day = func.timezone("UTC", func.date_trunc("day", func.timezone("UTC", Call.created_at)))
    day_rows = (
        await session.execute(
            select(day.label("day"), func.count())
            .select_from(Call)
            .where(*current_conds)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    type_rows = (
        await session.execute(
            select(InterventionEvent.type, func.count())
            .select_from(InterventionEvent)
            .join(Call, Call.id == InterventionEvent.call_id)
            .where(*current_conds)
            .group_by(InterventionEvent.type)
            .order_by(InterventionEvent.type)
        )
    ).all()
    return ok(
        HistoryReport(
            current=current,
            previous=previous,
            calls_per_day=[DayCount(day=d.date(), calls=n) for d, n in day_rows],
            interventions_by_type=[
                InterventionTypeCount(type=t, count=n) for t, n in type_rows
            ],
        )
    )


@router.get(
    "/analytics/filters",
    response_model=ResponseModel[ReportFilterOptions],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def report_filters(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[ReportFilterOptions]:
    """Filter options: the active provider catalog (global, non-PHI) and the tenant
    users who have initiated calls (workforce identity, not patient data)."""
    response.headers["Cache-Control"] = "no-store"
    providers = (
        await session.execute(
            select(InsuranceProvider.id, InsuranceProvider.name)
            .where(InsuranceProvider.status == ProviderStatus.ACTIVE.value)
            .order_by(InsuranceProvider.name)
        )
    ).all()
    vas = (
        await session.execute(
            select(AppUser.id, AppUser.name, AppUser.email)
            .join(Call, Call.initiated_by_id == AppUser.id)
            .distinct()
            .order_by(AppUser.name, AppUser.email)
        )
    ).all()
    return ok(
        ReportFilterOptions(
            providers=[FilterOption(id=i, name=n) for i, n in providers],
            vas=[FilterOption(id=i, name=name or email) for i, name, email in vas],
        )
    )
```

`Any` here needs `from typing import Any`. FastAPI parses `datetime` query params from ISO-8601 — timezone-aware values arrive as given; naive values would compare against timestamptz badly, so the frontend always sends `Z`-suffixed ISO strings (Task 6).

- [ ] **Step 4: Run to verify they pass**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_analytics.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full backend gate**

```bash
cd vera-backend && just check
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/analytics.py vera-backend/tests/
git commit -m "feat(analytics): history report and filter endpoints"
```

---

### Task 6: Frontend API module

**Files:**
- Create: `vera-frontend/src/lib/api/analytics.ts`
- Test: `vera-frontend/src/lib/api/analytics.test.ts`

**Interfaces:**
- Consumes: Tasks 3–5's endpoints; `apiRequest` from `@/lib/api/client`.
- Produces (consumed by Tasks 7–9): `QueueStatus`, `LivePanel`, `LiveProviderRow`, `HistoryReport`, `ReportMetrics`, `ReportParams`, `FilterOption`, `ReportFilterOptions` types and `getQueueStatus()`, `getLivePanel()`, `getHistoryReport(params)`, `getReportFilters()`.

- [ ] **Step 1: Write the failing test**

Mirror `src/lib/api/calls.test.ts`'s mocking style exactly (it mocks `@/lib/api/client`):

```typescript
import { beforeEach, describe, expect, it, vi } from "vitest"

import { apiRequest } from "@/lib/api/client"
import {
  getHistoryReport,
  getLivePanel,
  getQueueStatus,
  getReportFilters,
} from "@/lib/api/analytics"

vi.mock("@/lib/api/client", () => ({ apiRequest: vi.fn() }))

const mocked = vi.mocked(apiRequest)

describe("analytics api", () => {
  beforeEach(() => mocked.mockReset())

  it("GETs the queue status", async () => {
    const status = { limit: 3, active: 3, in_queue: 2 }
    mocked.mockResolvedValueOnce(status)
    await expect(getQueueStatus()).resolves.toEqual(status)
    expect(mocked).toHaveBeenCalledWith("/analytics/queue-status")
  })

  it("GETs the live panel", async () => {
    mocked.mockResolvedValueOnce({ rows: [] })
    await getLivePanel()
    expect(mocked).toHaveBeenCalledWith("/analytics/live")
  })

  it("GETs the report with only the provided filters", async () => {
    mocked.mockResolvedValueOnce({})
    await getHistoryReport({
      date_from: "2026-01-08T00:00:00.000Z",
      date_to: "2026-01-15T00:00:00.000Z",
      provider_id: "p-1",
    })
    const url = mocked.mock.calls[0][0] as string
    expect(url).toContain("/analytics/report?")
    expect(url).toContain("provider_id=p-1")
    expect(url).not.toContain("va_id")
  })

  it("GETs the filter options", async () => {
    mocked.mockResolvedValueOnce({ providers: [], vas: [] })
    await getReportFilters()
    expect(mocked).toHaveBeenCalledWith("/analytics/filters")
  })
})
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd vera-frontend && npm test -- analytics
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the module**

```typescript
import { apiRequest } from "@/lib/api/client"

/** GET /analytics/queue-status — tenant-wide mirror of the dispatcher's slot math. */
export type QueueStatus = { limit: number; active: number; in_queue: number }

export function getQueueStatus(): Promise<QueueStatus> {
  return apiRequest<QueueStatus>("/analytics/queue-status")
}

export type LiveProviderRow = {
  provider_id: string | null
  provider_name: string | null
  in_queue: number
  active: number
}

/** GET /analytics/live — live counts per provider, same rules as Live Monitoring. */
export type LivePanel = { rows: LiveProviderRow[] }

export function getLivePanel(): Promise<LivePanel> {
  return apiRequest<LivePanel>("/analytics/live")
}

export type ReportMetrics = {
  call_volume: number
  avg_duration_seconds: number | null
  avg_completion_pct: number | null
  intervened_calls: number
  intervention_rate: number | null
}

export type HistoryReport = {
  current: ReportMetrics
  previous: ReportMetrics
  calls_per_day: { day: string; calls: number }[]
  interventions_by_type: { type: string; count: number }[]
}

export type ReportParams = {
  date_from: string
  date_to: string
  provider_id?: string
  va_id?: string
}

/** GET /analytics/report — metrics for the range plus the previous equal-length range. */
export function getHistoryReport(params: ReportParams): Promise<HistoryReport> {
  const qs = new URLSearchParams({ date_from: params.date_from, date_to: params.date_to })
  if (params.provider_id) qs.set("provider_id", params.provider_id)
  if (params.va_id) qs.set("va_id", params.va_id)
  return apiRequest<HistoryReport>(`/analytics/report?${qs.toString()}`)
}

export type FilterOption = { id: string; name: string }
export type ReportFilterOptions = { providers: FilterOption[]; vas: FilterOption[] }

/** GET /analytics/filters — provider catalog + call-owning users for the dropdowns. */
export function getReportFilters(): Promise<ReportFilterOptions> {
  return apiRequest<ReportFilterOptions>("/analytics/filters")
}
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd vera-frontend && npm test -- analytics
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/api/analytics.ts vera-frontend/src/lib/api/analytics.test.ts
git commit -m "feat(fe): analytics API module"
```

---

### Task 7: Queue-limit card on Live Monitoring

**Files:**
- Create: `vera-frontend/src/components/monitoring/QueueLimitCard.tsx`
- Test: `vera-frontend/src/components/monitoring/QueueLimitCard.test.tsx`
- Modify: `vera-frontend/src/pages/LiveMonitoring.tsx`

**Interfaces:**
- Consumes: `QueueStatus` + `getQueueStatus()` (Task 6); `Card`/`CardContent` from `@/components/ui/card`.
- Produces: `<QueueLimitCard status={QueueStatus | null} />` rendered on Live Monitoring, refreshed by the page's existing 8-second poll.

- [ ] **Step 1: Write the failing component test**

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { QueueLimitCard } from "@/components/monitoring/QueueLimitCard"

describe("QueueLimitCard", () => {
  it("shows limit, active, and in-queue counts", () => {
    render(<QueueLimitCard status={{ limit: 3, active: 3, in_queue: 2 }} />)
    expect(screen.getByText("Active Call Queue Limit")).toBeInTheDocument()
    expect(screen.getByText("Active")).toBeInTheDocument()
    expect(screen.getByText("In Queue")).toBeInTheDocument()
    expect(screen.getByText("2")).toBeInTheDocument()
  })

  it("explains the wait when the limit is reached and calls are queued", () => {
    render(<QueueLimitCard status={{ limit: 3, active: 3, in_queue: 2 }} />)
    expect(screen.getByText(/queued calls start when a slot frees up/i)).toBeInTheDocument()
  })

  it("stays quiet below the limit", () => {
    render(<QueueLimitCard status={{ limit: 3, active: 1, in_queue: 0 }} />)
    expect(screen.queryByText(/slot frees up/i)).not.toBeInTheDocument()
  })

  it("renders nothing while the status is loading", () => {
    const { container } = render(<QueueLimitCard status={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd vera-frontend && npm test -- QueueLimitCard
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the card**

```tsx
import { Card, CardContent } from "@/components/ui/card"
import type { QueueStatus } from "@/lib/api/analytics"

/** Why a queued call hasn't started: the tenant-wide active-call limit (VR2-44). */
export function QueueLimitCard({ status }: { status: QueueStatus | null }) {
  if (!status) return null
  const atCapacity = status.active >= status.limit
  const figures = [
    { label: "Active Call Queue Limit", value: status.limit },
    { label: "Active", value: status.active },
    { label: "In Queue", value: status.in_queue },
  ]
  return (
    <Card size="sm">
      <CardContent className="flex flex-wrap items-center gap-x-10 gap-y-2">
        {figures.map(({ label, value }) => (
          <div key={label}>
            <p className="text-sm text-muted-foreground">{label}</p>
            <p className="text-2xl font-bold leading-tight">{value}</p>
          </div>
        ))}
        {atCapacity && status.in_queue > 0 && (
          <p className="text-sm text-amber-600">
            All call slots are in use — queued calls start when a slot frees up.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
```

(`Card` accepts `size="sm"` — see `src/components/ui/card.tsx`. If the compact variant renders oddly next to the page's stat cards, drop the prop.)

- [ ] **Step 4: Run to verify it passes**

```bash
cd vera-frontend && npm test -- QueueLimitCard
```

Expected: PASS.

- [ ] **Step 5: Wire it into Live Monitoring's existing poll**

In `src/pages/LiveMonitoring.tsx`:

1. Imports:

```tsx
import { QueueLimitCard } from "@/components/monitoring/QueueLimitCard"
import { getQueueStatus, type QueueStatus } from "@/lib/api/analytics"
```

2. State, next to the existing `stats` state (~line 127):

```tsx
const [queueStatus, setQueueStatus] = useState<QueueStatus | null>(null)
```

3. In the `load()` function's `Promise.allSettled` batch (~line 150), add `getQueueStatus()` as a fourth entry and handle its result the same way the siblings are handled (fulfilled → `setQueueStatus(value)`; rejected → leave the previous value, matching the page's "a hiccup must not stall the live list" comment):

```tsx
const [items, counts, past, queue] = await Promise.allSettled([
  listCalls(),
  getCallStats(),
  tab === "completed" ? listCalls("history") : Promise.resolve(null),
  getQueueStatus(),
])
...
if (queue.status === "fulfilled" && !cancelled) setQueueStatus(queue.value)
```

4. Render the card directly below the stat-card grid (~line 311), before the "Patient Call Status" heading:

```tsx
<QueueLimitCard status={queueStatus} />
```

- [ ] **Step 6: Run the page's tests and typecheck**

```bash
cd vera-frontend && npx tsc -b && npm test
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add vera-frontend/src/components/monitoring/QueueLimitCard.tsx vera-frontend/src/components/monitoring/QueueLimitCard.test.tsx vera-frontend/src/pages/LiveMonitoring.tsx
git commit -m "feat(fe): queue-limit card on Live Monitoring"
```

---

### Task 8: Analytics page + live provider panel

**Files:**
- Create: `vera-frontend/src/components/analytics/LiveAnalyticsPanel.tsx`
- Test: `vera-frontend/src/components/analytics/LiveAnalyticsPanel.test.tsx`
- Create: `vera-frontend/src/pages/Analytics.tsx`
- Modify: `vera-frontend/src/lib/nav.ts:35`
- Modify: `vera-frontend/src/App.tsx:31,90-93`

**Interfaces:**
- Consumes: `getLivePanel()` / `LivePanel` (Task 6); `Card`, `Table*` primitives; the nav/permission machinery (`visibleNavFor` reads `nav.ts`).
- Produces: `/analytics` renders `<Analytics />` for holders of `reports:dashboard`; `<LiveAnalyticsPanel />` polling every 8 s. Task 9 appends `<HistoryReport />` to this page.

- [ ] **Step 1: Write the failing component test**

```tsx
import { render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { LiveAnalyticsPanel } from "@/components/analytics/LiveAnalyticsPanel"
import { getLivePanel } from "@/lib/api/analytics"

vi.mock("@/lib/api/analytics", () => ({ getLivePanel: vi.fn() }))

const mocked = vi.mocked(getLivePanel)

describe("LiveAnalyticsPanel", () => {
  beforeEach(() => mocked.mockReset())

  it("renders one row per provider plus totals", async () => {
    mocked.mockResolvedValue({
      rows: [
        { provider_id: "a", provider_name: "Aetna", in_queue: 4, active: 2 },
        { provider_id: null, provider_name: null, in_queue: 2, active: 0 },
      ],
    })
    render(<LiveAnalyticsPanel />)
    await waitFor(() => expect(screen.getByText("Aetna")).toBeInTheDocument())
    expect(screen.getByText("(No provider)")).toBeInTheDocument()
    const totals = screen.getByTestId("live-totals")
    expect(totals).toHaveTextContent("6")
    expect(totals).toHaveTextContent("2")
  })

  it("shows an empty state when nothing is live", async () => {
    mocked.mockResolvedValue({ rows: [] })
    render(<LiveAnalyticsPanel />)
    await waitFor(() =>
      expect(screen.getByText(/no calls in queue or in progress/i)).toBeInTheDocument(),
    )
  })

  it("surfaces a load error", async () => {
    mocked.mockRejectedValue(new Error("boom"))
    render(<LiveAnalyticsPanel />)
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd vera-frontend && npm test -- LiveAnalyticsPanel
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the panel**

Copy Live Monitoring's polling conventions exactly (module-level `POLL_MS`, visibility check, `cancelled` flag):

```tsx
import { useEffect, useState } from "react"

import { Card } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { getLivePanel, type LivePanel } from "@/lib/api/analytics"
import { ApiError } from "@/lib/api/client"

// Same rhythm as Live Monitoring, so the two screens tick together.
const POLL_MS = 8000

/** VR2-44: live in-queue / active counts per provider, refreshed automatically. */
export function LiveAnalyticsPanel() {
  const [panel, setPanel] = useState<LivePanel | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await getLivePanel()
        if (!cancelled) {
          setPanel(data)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load live activity.")
        }
      }
    }
    void load()
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void load()
    }, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const rows = panel?.rows ?? []
  const totalQueued = rows.reduce((sum, r) => sum + r.in_queue, 0)
  const totalActive = rows.reduce((sum, r) => sum + r.active, 0)

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">Live Activity</h2>
      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Provider</TableHead>
              <TableHead>In Queue</TableHead>
              <TableHead>Active</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.provider_id ?? "none"}>
                <TableCell>{row.provider_name ?? "(No provider)"}</TableCell>
                <TableCell>{row.in_queue}</TableCell>
                <TableCell>{row.active}</TableCell>
              </TableRow>
            ))}
            {panel && rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={3} className="text-muted-foreground">
                  No calls in queue or in progress right now.
                </TableCell>
              </TableRow>
            )}
            {rows.length > 0 && (
              <TableRow data-testid="live-totals" className="font-medium">
                <TableCell>Total</TableCell>
                <TableCell>{totalQueued}</TableCell>
                <TableCell>{totalActive}</TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
```

- [ ] **Step 4: Create the page and mount it**

`src/pages/Analytics.tsx`:

```tsx
import { LiveAnalyticsPanel } from "@/components/analytics/LiveAnalyticsPanel"

export function Analytics() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Analytics</h1>
      <LiveAnalyticsPanel />
    </div>
  )
}
```

`src/App.tsx`: add `import { Analytics } from "@/pages/Analytics"` next to the other page imports, and swap the route element:

```tsx
<Route
  path="analytics"
  element={<RequireNavRoute to="/analytics"><Analytics /></RequireNavRoute>}
/>
```

`src/lib/nav.ts:35`: change the Analytics item's permission:

```ts
{ title: "Analytics", to: "/analytics", icon: BarChart3, permission: "reports:dashboard" },
```

Then check `src/lib/nav.test.ts` and `src/components/layout/Sidebar.test.ts` for assertions pinning the old `calls:read` gate on Analytics and update them.

- [ ] **Step 5: Run to verify everything passes**

```bash
cd vera-frontend && npx tsc -b && npm test
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vera-frontend/src/components/analytics/ vera-frontend/src/pages/Analytics.tsx vera-frontend/src/App.tsx vera-frontend/src/lib/nav.ts vera-frontend/src/lib/nav.test.ts
git commit -m "feat(fe): Analytics page with live provider panel behind reports:dashboard"
```

(Include `Sidebar.test.ts` in the add if it changed.)

---

### Task 9: History report UI (date presets, filters, metric cards, charts)

**Files:**
- Modify: `vera-frontend/package.json` (+ lockfile) — add `recharts`
- Create: `vera-frontend/src/lib/analytics/report.ts` + `report.test.ts`
- Create: `vera-frontend/src/components/analytics/MetricCard.tsx`
- Create: `vera-frontend/src/components/analytics/HistoryReport.tsx` + `HistoryReport.test.tsx`
- Modify: `vera-frontend/src/pages/Analytics.tsx`

**Interfaces:**
- Consumes: `getHistoryReport` / `getReportFilters` / types (Task 6); `Input`, `Select` (`@/components/ui/select` — the styled native select), `Card` primitives.
- Produces: `<HistoryReport />` below the live panel; pure helpers `presetRange`, `deltaPct`, `formatDuration`, `formatPct`.

- [ ] **Step 1: Install recharts**

```bash
cd vera-frontend && npm install recharts && npm ci
```

`npm ci` after the install is the repo rule for lockfile changes (pinned npm via Corepack — never regenerate the lock with a different npm). Expected: both succeed.

- [ ] **Step 2: Write the failing helpers test**

`src/lib/analytics/report.test.ts`:

```typescript
import { describe, expect, it } from "vitest"

import { deltaPct, formatDuration, formatPct, presetRange } from "@/lib/analytics/report"

const NOW = new Date("2026-08-04T10:30:00.000Z") // a Tuesday

describe("presetRange", () => {
  it("last 7 days ends now and starts 7 days earlier", () => {
    const { date_from, date_to } = presetRange("7d", NOW)
    expect(date_to).toBe("2026-08-04T10:30:00.000Z")
    expect(date_from).toBe("2026-07-28T10:30:00.000Z")
  })

  it("this week starts on Monday 00:00 UTC", () => {
    expect(presetRange("week", NOW).date_from).toBe("2026-08-03T00:00:00.000Z")
  })

  it("this month starts on the 1st 00:00 UTC", () => {
    expect(presetRange("month", NOW).date_from).toBe("2026-08-01T00:00:00.000Z")
  })
})

describe("deltaPct", () => {
  it("computes the percent change vs the previous period", () => {
    expect(deltaPct(120, 100)).toBeCloseTo(20)
    expect(deltaPct(80, 100)).toBeCloseTo(-20)
  })

  it("is null when either side is missing or previous is zero", () => {
    expect(deltaPct(null, 100)).toBeNull()
    expect(deltaPct(100, null)).toBeNull()
    expect(deltaPct(100, 0)).toBeNull()
  })
})

describe("formatters", () => {
  it("formats seconds as m/s and handles null", () => {
    expect(formatDuration(360)).toBe("6m 0s")
    expect(formatDuration(null)).toBe("—")
  })

  it("formats percentages from fractions and from 0-100 values", () => {
    expect(formatPct(0.25, { fraction: true })).toBe("25.0%")
    expect(formatPct(53.333)).toBe("53.3%")
    expect(formatPct(null)).toBe("—")
  })
})
```

- [ ] **Step 3: Run to verify it fails, then implement the helpers**

```bash
cd vera-frontend && npm test -- report
```

Expected: FAIL — module not found. Then create `src/lib/analytics/report.ts`:

```typescript
export type PresetKey = "7d" | "30d" | "90d" | "week" | "month" | "custom"

const DAY_MS = 86_400_000

/** UTC ranges so the buckets line up with the backend's UTC day convention. */
export function presetRange(
  preset: Exclude<PresetKey, "custom">,
  now: Date,
): { date_from: string; date_to: string } {
  const date_to = now.toISOString()
  switch (preset) {
    case "7d":
      return { date_from: new Date(now.getTime() - 7 * DAY_MS).toISOString(), date_to }
    case "30d":
      return { date_from: new Date(now.getTime() - 30 * DAY_MS).toISOString(), date_to }
    case "90d":
      return { date_from: new Date(now.getTime() - 90 * DAY_MS).toISOString(), date_to }
    case "week": {
      const mondayOffset = (now.getUTCDay() + 6) % 7
      const monday = Date.UTC(
        now.getUTCFullYear(),
        now.getUTCMonth(),
        now.getUTCDate() - mondayOffset,
      )
      return { date_from: new Date(monday).toISOString(), date_to }
    }
    case "month": {
      const first = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)
      return { date_from: new Date(first).toISOString(), date_to }
    }
  }
}

export function deltaPct(current: number | null, previous: number | null): number | null {
  if (current === null || previous === null || previous === 0) return null
  return ((current - previous) / previous) * 100
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—"
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

export function formatPct(value: number | null, opts?: { fraction?: boolean }): string {
  if (value === null) return "—"
  const pct = opts?.fraction ? value * 100 : value
  return `${pct.toFixed(1)}%`
}
```

Re-run `npm test -- report`. Expected: PASS.

- [ ] **Step 4: Implement the MetricCard**

`src/components/analytics/MetricCard.tsx`:

```tsx
import { TrendingDown, TrendingUp } from "lucide-react"

import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"

type Props = {
  label: string
  value: string
  deltaPct: number | null
  /** For metrics where DOWN is the good direction (e.g. intervention rate). */
  invert?: boolean
}

export function MetricCard({ label, value, deltaPct, invert = false }: Props) {
  const up = deltaPct !== null && deltaPct >= 0
  const good = deltaPct === null ? true : up !== invert
  return (
    <Card size="sm">
      <CardContent>
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className="text-2xl font-bold leading-tight">{value}</p>
        {deltaPct !== null && (
          <p className={cn("mt-1 text-sm", good ? "text-emerald-600" : "text-red-600")}>
            {up ? (
              <TrendingUp aria-label="up" className="mr-1 inline size-4" />
            ) : (
              <TrendingDown aria-label="down" className="mr-1 inline size-4" />
            )}
            {Math.abs(deltaPct).toFixed(1)}% vs previous period
          </p>
        )}
      </CardContent>
    </Card>
  )
}
```

- [ ] **Step 5: Write the failing HistoryReport test**

`src/components/analytics/HistoryReport.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { HistoryReport } from "@/components/analytics/HistoryReport"
import { getHistoryReport, getReportFilters } from "@/lib/api/analytics"

vi.mock("@/lib/api/analytics", () => ({
  getHistoryReport: vi.fn(),
  getReportFilters: vi.fn(),
}))

const mockedReport = vi.mocked(getHistoryReport)
const mockedFilters = vi.mocked(getReportFilters)

const REPORT = {
  current: {
    call_volume: 40,
    avg_duration_seconds: 360,
    avg_completion_pct: 53.3,
    intervened_calls: 10,
    intervention_rate: 0.25,
  },
  previous: {
    call_volume: 20,
    avg_duration_seconds: 300,
    avg_completion_pct: 50,
    intervened_calls: 8,
    intervention_rate: 0.4,
  },
  calls_per_day: [{ day: "2026-08-01", calls: 40 }],
  interventions_by_type: [{ type: "whisper", count: 10 }],
}

describe("HistoryReport", () => {
  beforeEach(() => {
    mockedReport.mockReset()
    mockedFilters.mockReset()
    mockedReport.mockResolvedValue(REPORT)
    mockedFilters.mockResolvedValue({
      providers: [{ id: "p1", name: "Aetna" }],
      vas: [{ id: "u1", name: "Sam VA" }],
    })
  })

  it("loads and renders the metric cards", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(screen.getByText("40")).toBeInTheDocument())
    expect(screen.getByText("Completion %")).toBeInTheDocument()
    expect(screen.getByText("53.3%")).toBeInTheDocument()
    expect(screen.getByText("Intervention Rate")).toBeInTheDocument()
    expect(screen.getByText("25.0%")).toBeInTheDocument()
    expect(screen.getByText("6m 0s")).toBeInTheDocument()
  })

  it("refetches when the provider filter changes", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(mockedReport).toHaveBeenCalledTimes(1))
    await userEvent.selectOptions(screen.getByLabelText(/provider/i), "p1")
    await waitFor(() => expect(mockedReport).toHaveBeenCalledTimes(2))
    expect(mockedReport.mock.calls[1][0]).toMatchObject({ provider_id: "p1" })
  })

  it("shows custom date inputs only for the custom preset", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(mockedReport).toHaveBeenCalled())
    expect(screen.queryByLabelText(/from date/i)).not.toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText(/date range/i), "custom")
    expect(screen.getByLabelText(/from date/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 6: Run to verify it fails, then implement HistoryReport**

```bash
cd vera-frontend && npm test -- HistoryReport
```

Expected: FAIL — module not found. Then create `src/components/analytics/HistoryReport.tsx`:

```tsx
import { useEffect, useState } from "react"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

import { MetricCard } from "@/components/analytics/MetricCard"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import {
  getHistoryReport,
  getReportFilters,
  type HistoryReport as HistoryReportData,
  type ReportFilterOptions,
} from "@/lib/api/analytics"
import { ApiError } from "@/lib/api/client"
import {
  deltaPct,
  formatDuration,
  formatPct,
  presetRange,
  type PresetKey,
} from "@/lib/analytics/report"

const PRESETS: { key: PresetKey; label: string }[] = [
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "90d", label: "Last 90 days" },
  { key: "week", label: "This week" },
  { key: "month", label: "This month" },
  { key: "custom", label: "Custom range" },
]

const BAR_COLOR = "#34B2B2" // brand teal, same literal Live Monitoring uses

/** VR2-45: historical metrics with previous-period deltas and charts. */
export function HistoryReport() {
  const [preset, setPreset] = useState<PresetKey>("30d")
  const [customFrom, setCustomFrom] = useState("")
  const [customTo, setCustomTo] = useState("")
  const [providerId, setProviderId] = useState("")
  const [vaId, setVaId] = useState("")
  const [filters, setFilters] = useState<ReportFilterOptions | null>(null)
  const [report, setReport] = useState<HistoryReportData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getReportFilters().then(setFilters).catch(() => setFilters({ providers: [], vas: [] }))
  }, [])

  useEffect(() => {
    let cancelled = false
    const range =
      preset === "custom"
        ? customFrom && customTo
          ? // UTC-day widening, same convention as CallHistory's date filters.
            { date_from: `${customFrom}T00:00:00Z`, date_to: `${customTo}T23:59:59Z` }
          : null
        : presetRange(preset, new Date())
    if (!range) return
    getHistoryReport({
      ...range,
      provider_id: providerId || undefined,
      va_id: vaId || undefined,
    })
      .then((data) => {
        if (!cancelled) {
          setReport(data)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load the report.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [preset, customFrom, customTo, providerId, vaId])

  const cur = report?.current
  const prev = report?.previous

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">History Report</h2>
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="report-range">Date range</Label>
          <Select
            id="report-range"
            value={preset}
            onChange={(e) => setPreset(e.target.value as PresetKey)}
          >
            {PRESETS.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </Select>
        </div>
        {preset === "custom" && (
          <>
            <Input
              type="date"
              aria-label="From date"
              value={customFrom}
              onChange={(e) => setCustomFrom(e.target.value)}
              className="w-40"
            />
            <Input
              type="date"
              aria-label="To date"
              value={customTo}
              onChange={(e) => setCustomTo(e.target.value)}
              className="w-40"
            />
          </>
        )}
        <div className="grid gap-1.5">
          <Label htmlFor="report-provider">Provider</Label>
          <Select
            id="report-provider"
            value={providerId}
            onChange={(e) => setProviderId(e.target.value)}
          >
            <option value="">All providers</option>
            {filters?.providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="report-va">VA</Label>
          <Select id="report-va" value={vaId} onChange={(e) => setVaId(e.target.value)}>
            <option value="">All VAs</option>
            {filters?.vas.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </Select>
        </div>
      </div>
      {cur && prev && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Call Volume"
            value={String(cur.call_volume)}
            deltaPct={deltaPct(cur.call_volume, prev.call_volume)}
          />
          <MetricCard
            label="Completion %"
            value={formatPct(cur.avg_completion_pct)}
            deltaPct={deltaPct(cur.avg_completion_pct, prev.avg_completion_pct)}
          />
          <MetricCard
            label="Intervention Rate"
            value={formatPct(cur.intervention_rate, { fraction: true })}
            deltaPct={deltaPct(cur.intervention_rate, prev.intervention_rate)}
            invert
          />
          <MetricCard
            label="Avg Call Duration"
            value={formatDuration(cur.avg_duration_seconds)}
            deltaPct={deltaPct(cur.avg_duration_seconds, prev.avg_duration_seconds)}
          />
        </div>
      )}
      {report && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Calls per day</CardTitle>
            </CardHeader>
            <CardContent className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={report.calls_per_day}>
                  <CartesianGrid vertical={false} strokeOpacity={0.3} />
                  <XAxis dataKey="day" tickLine={false} axisLine={false} fontSize={12} />
                  <YAxis allowDecimals={false} tickLine={false} axisLine={false} fontSize={12} />
                  <Tooltip cursor={{ fillOpacity: 0.1 }} />
                  <Bar dataKey="calls" fill={BAR_COLOR} radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Interventions by type</CardTitle>
            </CardHeader>
            <CardContent className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={report.interventions_by_type} layout="vertical">
                  <CartesianGrid horizontal={false} strokeOpacity={0.3} />
                  <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} fontSize={12} />
                  <YAxis type="category" dataKey="type" width={90} tickLine={false} axisLine={false} fontSize={12} />
                  <Tooltip cursor={{ fillOpacity: 0.1 }} />
                  <Bar dataKey="count" fill={BAR_COLOR} radius={[0, 3, 3, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </div>
      )}
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
```

Check `src/components/ui/select.tsx`'s actual export/prop shape (it's a styled native `<select>`) and adapt the `<Select>` usage to it — if it doesn't forward `id`/`onChange` as written, use whatever an existing page (e.g. `DataManagement.tsx`'s status filter) does. Chart styling: single-hue brand teal for single-series bars, no legend, muted grid — matching the app's restrained visual language.

- [ ] **Step 7: Mount it on the page**

In `src/pages/Analytics.tsx`, add below the live panel:

```tsx
import { HistoryReport } from "@/components/analytics/HistoryReport"
...
      <LiveAnalyticsPanel />
      <HistoryReport />
```

- [ ] **Step 8: Run the tests**

```bash
cd vera-frontend && npm test -- HistoryReport && npx tsc -b
```

Expected: PASS. (ResponsiveContainer renders empty at zero size in jsdom — the tests assert cards and controls, not chart internals; that's expected.)

- [ ] **Step 9: Commit**

```bash
git add vera-frontend/package.json vera-frontend/package-lock.json vera-frontend/src/lib/analytics/ vera-frontend/src/components/analytics/ vera-frontend/src/pages/Analytics.tsx
git commit -m "feat(fe): history report with metric cards, filters, and charts"
```

---

### Task 10: Full gates, simplify pass, final verification

**Files:** none new — verification and cleanup only.

- [ ] **Step 1: Backend full gate**

```bash
cd vera-backend && just check
```

Expected: ruff (check + format), mypy --strict, pytest all green.

- [ ] **Step 2: Frontend full gate**

```bash
cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build
```

Expected: all four green.

- [ ] **Step 3: Manual demo pass**

`just up` + `just migrate` + `just api` (backend) and `npm run dev` (frontend); seed demo data (`just seed`, `just test_seed_patient_data`). Verify by hand: the Analytics tab appears for an admin and not for a VA; the live panel numbers equal the Live Monitoring counts while a form sits in queue; the queue-limit card shows the tenant ceiling; the report renders with each preset and filter. (If Azad prefers to drive the servers himself, hand this step to him.)

- [ ] **Step 4: Run the simplify pass (repo-mandated)**

Run the repo's mandated post-implementation simplify pass over the changed files (repo-root `CLAUDE.md`: trigger the code-simplifier / `/simplify` flow). Apply its refinements.

- [ ] **Step 5: Re-run both gates on the exact final tree**

```bash
cd vera-backend && just check
cd ../vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build
```

Expected: all green (mandatory after simplify touches anything).

- [ ] **Step 6: Stage everything and stop for review**

```bash
git add -A
git status
```

Leave the work staged and wait for Azad's review before any final commit/push (his standing rule: he reviews the staged diff in the IDE first). Per-task commits made along the way are fine; do not push.

---

## Spec traceability

| Plan-doc requirement | Where satisfied |
|---|---|
| `reports:dashboard` permission, role-editable, RLS-enforced | Task 1 (catalog + seed), Tasks 3–5 (`require(...)` + `TenantSession`), Task 8 (nav gate) |
| Queue-limit card on Live Monitoring (limit / active / in queue) | Task 3 (endpoint), Task 7 (card) |
| Live panel table per provider, `(No provider)` bucket, totals add up | Task 4 (endpoint), Task 8 (panel) |
| Panel always matches Live Monitoring (same statuses, same visibility) | Task 4 (shared `ACTIVE_CALL_STATUSES` + `visible_to`, consistency test) |
| 8-second auto-refresh | Tasks 7–8 (existing poll / `POLL_MS = 8000`) |
| Date presets + custom range; provider + VA filters | Task 5 (params + filters endpoint), Task 9 (UI) |
| Metric cards with previous-period comparison + trend arrows | Task 5 (`current`/`previous`), Task 9 (`MetricCard`, `deltaPct`) |
| Completion % frozen at call end (honest history) | Task 2 (freeze + backfill), Task 5 (terminal-only average) |
| Intervention rate from permanent event rows | Task 5 (`InterventionEvent` distinct-call count) |
| Calls-per-day chart + intervention breakdown | Task 5 (series), Task 9 (recharts) |
| Accuracy: computed from raw rows, spot-check matches | Task 5 (no precomputation; hand-computed seed test) |
| Tenant isolation + access tests | Tasks 3–5 (integration tests) |
| IVR success / cost per call / latency recording | **Out of scope** — plan-doc Step 3, separate plan |
