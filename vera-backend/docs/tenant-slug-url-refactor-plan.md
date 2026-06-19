# Tenant Slug URL Refactor — Implementation Plan

Companion to `tenant-slug-url-refactor-analysis.md` (the spec/rationale). Sequenced so each
task leaves the tree green (`just check` = ruff + mypy --strict + pytest). Tasks are
dependency-ordered; execute one at a time.

Pre-launch, no live data: no Redis-session back-compat, no data migration — re-login /
`just up` regenerates sessions. Each task owns the test/fixture updates for the code it
changes, so the suite is green at the end of every task.

## Global Constraints (bind every task)

- **`account_type` (`'tenant'`/`'platform'`) is the definitive platform-vs-tenant signal.**
  Branch on `account_type`; treat `tenant_id` nullability as an invariant to assert and
  **fail closed (401) on mismatch** — never branch on `tenant_id is None`. (control_plane
  CLAUDE.md, "Platform vs tenant identity".)
- **DB clock only** — timestamps/audit/elevation times via Postgres `now()`, never Python.
- **PEP 695 type params** (`class Foo[T]`); ruff rejects `Generic[T]`/`TypeVar`.
- **asyncio only** — no `anyio` import, no new `anyio` dependency.
- **Errors:** `raise CustomAPIException`/subclasses, never `HTTPException`, in `api/v1` route
  code. (The dependency layer in `deps.py`/`rbac.py` currently raises `HTTPException` — match
  the surrounding file's existing pattern; do not introduce a new style.)
- **Single active elevation grant per operator** is a DB-enforced invariant the resolver
  relies on — cite it in a comment at the operator-only grant lookup.
- `just check` must pass at the end of each task.

---

## Task 1: `AccountType` enum + model typing

**Files:** `packages/vera_core/src/vera_core/models/enums.py`,
`packages/vera_core/src/vera_core/models/app_user.py`

1. In `enums.py` add:
   ```python
   class AccountType(enum.StrEnum):
       TENANT = "tenant"
       PLATFORM = "platform"
   ```
2. In `app_user.py`, reference `AccountType.TENANT.value` for the column `default` (keep the
   column `String(16)`; the existing CHECK `account_type IN ('tenant','platform')` already
   constrains values). No migration — values unchanged.
3. Export `AccountType` from `vera_core.models` if that package re-exports enums (match how
   `FormStatus`/`CallStatus` are exported).

**Tests:** none new (no behavior change); `just check` green.

---

## Task 2: carry `account_type` through `SessionData` + `VerifiedIdentity`

**Files:** `auth/session.py`, `auth/identity.py`, `api/v1/auth.py`, and any test fixtures
that build `SessionData`/`VerifiedIdentity`.

1. `auth/identity.py` — `VerifiedIdentity`: add `account_type: AccountType` (required field).
2. `auth/session.py` — `SessionData`: add `account_type: str` (required). Emit it in
   `to_dict`; read it as a required key in `from_dict` (no `.get` default). Update the
   `_ABS_SENTINEL`/demo `SessionData` (~line 99) to pass `account_type`.
3. `auth/session.py` — `SessionVerifier.verify` (~line 248): pass
   `account_type=AccountType(data.account_type)` into `VerifiedIdentity`.
4. `api/v1/auth.py`:
   - `_PasswordCreds`: add `account_type: str`.
   - `_load_password_creds`: select `AppUser.account_type` and populate it.
   - `SessionData(...)` mint (~line 273): set `account_type=creds.account_type`.
   - `/me` (~line 436/460): read `account_type` from `identity`; drop `account_type` from the
     `select(AppUser.name, AppUser.account_type)` (still fetch `name`).
5. Update every test fixture / helper that constructs `SessionData` or `VerifiedIdentity` to
   supply `account_type` (grep both names across tests).

**Tests:** existing suite passes with the new required field threaded through; add a focused
test that `SessionData.from_dict` raises on a payload missing `account_type` (required-key
guard). `just check` green. Behavior otherwise identical — the resolver still keys off
`tenant_id` until Task 4.

---

## Task 3: `TenantContext` + `tenant_context` resolver (TDD)

**Files:** `auth/elevation.py`, `deps.py`, plus a new/existing resolver test module.

1. `auth/elevation.py`: add
   ```python
   async def active_grant_for_operator(session, *, operator: UUID) -> ElevationGrant | None:
       """The operator's single active grant, or None. Relies on the DB unique constraint
       that an operator holds AT MOST ONE active grant — so the operating tenant is
       unambiguous without a target in the request. If that constraint is ever relaxed,
       callers MUST pass an explicit target selector instead of trusting grants[0]."""
       grants = await active_grants(session, super_admin_user_id=operator)
       return grants[0] if grants else None
   ```
2. `deps.py`: add `TenantContext` (frozen dataclass: `tenant_id: UUID`,
   `elevation_grant_id: UUID | None`) and `async def tenant_context(...)`:
   - `identity.account_type is AccountType.TENANT`: assert `tenant_id is not None` else
     401 "malformed session"; return `TenantContext(identity.tenant_id, None)`.
   - else (PLATFORM): assert `tenant_id is None` else 401; look up
     `active_grant_for_operator`; 403 "no active elevation" if none; stamp
     `request.state.vera_elevation = grant.id`; return
     `TenantContext(grant.target_tenant_id, grant.id)`.

**Tests (write first, RED→GREEN):**
- tenant user → `(tenant_id, None)`.
- tenant user with `tenant_id=None` → 401.
- platform operator + active grant → `(target_tenant_id, grant_id)` and `vera_elevation`
  stamped.
- platform operator, no grant → 403.
- platform operator with non-null `tenant_id` → 401.

Resolver not wired into routes yet. `just check` green.

---

## Task 4: rewire the chain onto `tenant_context`; delete `tenant_guard`

**Files:** `auth/rbac.py`, `deps.py`, delete `auth/tenant_guard.py` + its test module.

1. `auth/rbac.py` — `require().dependency`: replace
   `tenant_id: Annotated[UUID, Depends(tenant_guard)]` with
   `ctx: Annotated[TenantContext, Depends(tenant_context)]`; use `ctx.tenant_id`.
2. `deps.py` — `tenant_scoped_session`: drop the `tenant_slug` param; depend on
   `tenant_context`; use `tenant_session(ctx.tenant_id)` when `ctx.elevation_grant_id is
   None`, else `elevated_session(ctx.tenant_id)`. Remove the in-body `resolve_tenant` /
   `resolve_elevation` calls.
3. Delete `auth/tenant_guard.py` and its tests; remove all imports of `tenant_guard`.
4. `deps.py`: delete `resolve_elevation` if now unreferenced (its logic moved into
   `tenant_context`). KEEP `resolve_tenant`/`resolve_tenant_id` — pre-auth routes use them.

**Note:** routes still carry `{tenant_slug}` in their path strings here; the chain now ignores
it. Existing tenant-user integration tests still hit the slug URLs and pass (FastAPI matches
the path param; the chain derives tenant from the session). `just check` green.

---

## Task 5: strip `{tenant_slug}` from authenticated routes + update their tests

**Files:** `api/v1/{auth,users,roles,api_keys,calls,providers}.py` and the integration tests
hitting those routes.

1. Rewrite the authenticated route path strings per the table in the analysis doc, removing
   the `/tenants/{tenant_slug}` prefix. **Leave the 4 pre-auth `auth.py` routes** (`login`,
   `mfa/verify`, `invitations/accept`, `invitations/activate-mfa`) on their slug paths.
2. `users.py` `invite_user`: remove the `tenant_slug: str` param; build the invite URL from
   `caller.tenant_slug`.
3. Grep to confirm no authenticated route signature still declares `tenant_slug: str`.
4. Update every integration test hitting these routes to the new flat paths; keep pre-auth
   tests on slug paths.

**Tests:** full integration suite updated and green. `just check` green.

---

## Task 6: verification + cleanup

1. Confirm the elevated-operator integration path end-to-end: create a grant via
   `/platform/elevations`, call a flat tenant route as the operator, assert it resolves the
   target tenant and stamps `audit_log.elevation_session_id`. Add the test if absent.
2. Run `/simplify` on the whole diff (repo CLAUDE.md requirement), then `just check`.
3. Confirm `account_type` is set on the only session-mint path and `from_dict` rejects a
   payload without it (regression guard for a future platform-login mint path).

**Tests:** `just check` green; `/simplify` applied; ready for the final whole-branch review.
