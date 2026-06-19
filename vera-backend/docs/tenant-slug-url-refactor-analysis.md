# Tenant Slug URL Refactor — Analysis

## Problem

All authenticated API endpoints embed `{tenant_slug}` in the URL path
(`/api/v1/tenants/{tenant_slug}/...`). After login, the opaque session token already
encodes the caller's tenant. Requiring clients to repeat the slug on every request is
redundant friction, and the slug in the URL is only ever used as a cross-check against
the session — never as a primary identifier for an authenticated caller.

## How tenant identity is resolved today

`VerifiedIdentity` (`auth/identity.py`) carries `user_id`, `email`, `tenant_id`
(`UUID | None`), and `tenant_slug`, hydrated from the Redis `SessionData` at verify time.
The authz chain is:

```
current_identity (401) ─► tenant_guard (403) ─► require(permission) (403) ─► tenant_scoped_session (RLS)
```

`require()` (`auth/rbac.py`) is what the routes actually declare; it internally composes
`Depends(tenant_guard)` + `Depends(tenant_scoped_session)`. Both of those read the URL
`{tenant_slug}` (FastAPI injects it from the path). `tenant_guard` then:

- **Tenant user** — string-compares the URL slug against `identity.tenant_slug` (fast path,
  no DB), or DB-resolves for a slug-less session; a mismatch is a cross-tenant probe → 403.
- **Platform operator** (`tenant_id is None`) — DB-resolves the slug and allows only behind
  an active elevation grant into that exact tenant.

**The routes themselves never read the slug** (verified: `calls.py`, `api_keys.py`,
`roles.py`, `providers.py`, and `users.py` except `invite_user`). They only carry it in the
route *path string*. So the slug is consumed entirely inside the chain — which means fixing
the chain fixes every route at once.

---

## Two corrections that shape the design

### 1. The change surface is central, not per-route

Because routes only declare `require(...)` / `Depends(tenant_scoped_session)` and never read
the slug, the refactor is **two central edits** (point `require` and `tenant_scoped_session`
at a new resolver) plus mechanical path-string changes — not 13 per-function rewrites. The
only route that reads the slug as a *value* is `invite_user` (builds the invite link), and
`caller.tenant_slug` is already in its session.

### 2. `account_type` is the definitive platform/tenant signal — not `tenant_id is None`

`app_user.account_type` (`'tenant'` | `'platform'`) is the field that *means* "platform
operator". A DB CHECK (`tenant_binding`) pairs it with `tenant_id` nullability
(`platform ⟺ tenant_id IS NULL`) **at rest**. But the resolver reads a session minted in
code and round-tripped through Redis, not the DB row — so `tenant_id is None` is only a
*structural proxy*. It misfires the moment the two diverge:

- The codebase already mints "slug-less sessions" (the `tenant_guard` slow path). A future
  tenant session minted without a `tenant_id` (a minting bug) would be **silently classified
  as a platform operator** and routed into the elevation branch — a privilege-escalation-
  shaped misroute.
- "Platform operator" (SUPER_ADMIN) is a real account property with its own power; it should
  be read from the field that means it, asserting `tenant_id` consistency, not inferred from
  the absence of another field.

**`account_type` is not in `SessionData` or `VerifiedIdentity` today** — `/me` re-queries it
from the DB (`auth.py:436`). Carrying it in the session is a prerequisite of this refactor.

---

## The key invariant that makes one unified resolver work — including SUPER_ADMIN

A platform operator holds **at most one active elevation grant** (the DB
`create_elevation_grant` raises a unique violation → 409 "operator already holds an active
elevation"). The grant carries `target_tenant_id`. So the operating tenant is **always
derivable from the session**, never needed in the URL — even for elevation:

- **Tenant user** → operating tenant = `identity.tenant_id`.
- **Platform operator** → operating tenant = their single active grant's `target_tenant_id`.

The slug was only ever a cross-check, redundant in both cases. Nothing is lost by dropping it.

---

## The unified resolver

One dependency subsumes `tenant_guard` + `resolve_elevation` + the tenant-selection half of
`tenant_scoped_session`:

```python
@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    elevation_grant_id: UUID | None   # None = tenant user; set = elevated operator

async def tenant_context(request, identity, sessionmaker) -> TenantContext:
    if identity.account_type is AccountType.TENANT:
        if identity.tenant_id is None:                 # invariant broken → corrupted session
            raise HTTPException(401, "malformed session")   # fail closed, never guess
        return TenantContext(identity.tenant_id, None)

    # account_type is PLATFORM (SUPER_ADMIN): no home tenant, elevation-gated
    if identity.tenant_id is not None:                  # invariant broken the other way
        raise HTTPException(401, "malformed session")
    async with sessionmaker() as session:
        grant = await elevation.active_grant_for_operator(session, operator=identity.user_id)
    if grant is None:
        raise HTTPException(403, "no active elevation")
    request.state.vera_elevation = grant.id            # keeps current_elevation() working
    return TenantContext(grant.target_tenant_id, grant.id)
```

- `account_type` is the definitive branch; `tenant_id` is an asserted invariant (fail-closed
  on mismatch), not the sole signal.
- FastAPI caches a dependency once per request, so `require()` and `tenant_scoped_session`
  share this single resolution — removing today's double slug+grant resolution.
- `current_elevation(request)` still reads `request.state.vera_elevation` for audit stamping,
  unchanged.

Chain after the refactor:

- `require()` → `tenant_id = ctx.tenant_id`
- `tenant_scoped_session` → `tenant_session(...)` if `grant_id is None` else `elevated_session(...)`

A bonus security property: with no slug in the URL, the cross-tenant probe surface disappears
**by construction** — a caller cannot name a tenant that isn't theirs, so the "tenant
mismatch" 403 branch becomes unreachable rather than guarded-against. The RLS backstop
(`SET LOCAL app.tenant_id` from the verified id / grant) is untouched.

---

## Endpoints that MUST keep `tenant_slug` in the URL (pre-auth)

No session exists yet, so the slug is the only tenant identifier. They call
`resolve_tenant_id()` directly (RLS not yet in scope).

| Method | Path |
|--------|------|
| `POST` | `/api/v1/tenants/{tenant_slug}/auth/login` |
| `POST` | `/api/v1/tenants/{tenant_slug}/auth/mfa/verify` |
| `POST` | `/api/v1/tenants/{tenant_slug}/auth/invitations/accept` |
| `POST` | `/api/v1/tenants/{tenant_slug}/auth/invitations/activate-mfa` |

---

## Endpoints that DROP `tenant_slug` (authenticated)

All use `require(...)` (→ tenant guard + scoped session), so the tenant comes from the
session via `tenant_context`.

| Current path | Proposed path | File |
|-------------|---------------|------|
| `POST /tenants/{tenant_slug}/auth/mfa/enroll` | `POST /auth/mfa/enroll` | `auth.py` |
| `POST /tenants/{tenant_slug}/auth/mfa/activate` | `POST /auth/mfa/activate` | `auth.py` |
| `POST /tenants/{tenant_slug}/users/invitations` | `POST /users/invitations` | `users.py` † |
| `GET  /tenants/{tenant_slug}/users` | `GET  /users` | `users.py` |
| `POST /tenants/{tenant_slug}/users/{user_id}/deactivate` | `POST /users/{user_id}/deactivate` | `users.py` |
| `GET  /tenants/{tenant_slug}/roles` | `GET  /roles` | `roles.py` |
| `POST /tenants/{tenant_slug}/roles` | `POST /roles` | `roles.py` |
| `POST /tenants/{tenant_slug}/users/{user_id}/roles` | `POST /users/{user_id}/roles` | `roles.py` |
| `DELETE /tenants/{tenant_slug}/users/{user_id}/roles/{role_id}` | `DELETE /users/{user_id}/roles/{role_id}` | `roles.py` |
| `POST /tenants/{tenant_slug}/api-keys` | `POST /api-keys` | `api_keys.py` |
| `GET  /tenants/{tenant_slug}/api-keys` | `GET  /api-keys` | `api_keys.py` |
| `GET  /tenants/{tenant_slug}/api-keys/scopes` | `GET  /api-keys/scopes` | `api_keys.py` |
| `POST /tenants/{tenant_slug}/api-keys/{key_id}/revoke` | `POST /api-keys/{key_id}/revoke` | `api_keys.py` |
| `GET  /tenants/{tenant_slug}/calls` | `GET  /calls` | `calls.py` |
| `GET  /tenants/{tenant_slug}/auth/providers` | `GET  /auth/providers` | `providers.py` |
| `PATCH /tenants/{tenant_slug}/auth/providers/{provider_type}` | `PATCH /auth/providers/{provider_type}` | `providers.py` |

† `invite_user` reads `tenant_slug` to build the invite URL (`auth.py`-style line 154) —
switch to `caller.tenant_slug` from the session.

---

## Open item: platform-operator login

There is currently **no mint path that produces a `tenant_id=None` session** — the only
`SessionData` construction (`auth.py:273`) is the tenant login flow. Platform-operator login
is not yet implemented. Wherever it lands, it must set `account_type='platform'` and
`tenant_id=None`. Until then, the platform branch of `tenant_context` is exercised only by
tests, but it must be correct now so the elevation flow works the day platform login ships.

---

## Files touched

| File | Change |
|------|--------|
| `vera_core/models/enums.py` | add `AccountType('tenant'/'platform')` StrEnum |
| `vera_core/models/app_user.py` | type `account_type` against the enum (CHECK already exists) |
| `auth/session.py` | add `account_type` to `SessionData` (+ `to_dict`/`from_dict`) |
| `api/v1/auth.py` | load `AppUser.account_type` in `_PasswordCreds`; set it on `SessionData`; `/me` reads from identity |
| `auth/identity.py` | add `account_type` to `VerifiedIdentity` |
| `auth/session.py` (verify) | surface `account_type` in `SessionVerifier.verify` |
| `deps.py` | add `TenantContext` + `tenant_context`; rewrite `tenant_scoped_session` to consume it; drop the slug param |
| `auth/rbac.py` | `require()` depends on `tenant_context` instead of `tenant_guard` |
| `auth/tenant_guard.py` | **deleted** (logic absorbed into `tenant_context`) |
| `auth/elevation.py` | add `active_grant_for_operator(session, operator)` (operator-only lookup, no target tenant) |
| `api/v1/{auth,users,roles,api_keys,calls,providers}.py` | strip `/tenants/{tenant_slug}` from authenticated route paths |
| `api/v1/users.py` | `invite_user`: `tenant_slug` → `caller.tenant_slug` |
| tests | update route paths; add `tenant_context` cases (tenant, elevated operator, no grant, invariant violation) |

`resolve_tenant` / `resolve_tenant_id` (slug→UUID) survive — used only by the 4 pre-auth
routes, which never enter this chain.
