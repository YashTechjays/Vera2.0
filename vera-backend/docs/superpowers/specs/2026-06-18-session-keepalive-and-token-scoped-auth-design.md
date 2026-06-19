# Session keepalive + token-scoped self-session endpoints — design

Date: 2026-06-18 · Status: Approved (design); pending implementation plan

## Summary

Add an idle/inactivity auto-logout capability to the opaque Redis session model,
plus a hard absolute session cap, and move the three "self-session" endpoints
(`/me`, `/logout`, and the new `/keepalive`) off the tenant-slug URL path onto a
token-authenticated `/auth/...` surface so they work for both tenant users and
platform operators (`tenant_id = None`).

No database migration is required — the change is config + Redis store methods +
routing only.

## Background & motivation

### The session today

- Login mints an opaque token (`auth/session.py`): `SET vera:sess:{token} <data> EX session_ttl_seconds`.
  The token is a random Redis key carrying no claims; the client cannot read it.
- `session_ttl_seconds` (default 3600) is set **once at login and never touched
  again**, so the session is effectively a **fixed 1-hour absolute timeout** — it
  expires an hour after login regardless of activity. There is no sliding/idle
  behavior and no refresh mechanism.
- `SessionVerifier.verify` resolves a bearer token to a `VerifiedIdentity` and
  rejects any non-`mfa_passed` token. This runs on every authenticated request.

### What we want

A HIPAA-style automatic logoff driven by inactivity, with a frontend "Stay logged
in?" prompt at the boundary. The frontend integration is **out of scope for this
spec** (a fixed 14-min timer + 1-min grace was discussed and will be designed
later). This spec covers only the backend primitive the frontend needs: a way to
**extend (slide) an existing session's lifetime**, bounded by a hard maximum.

Key reframing: idle auto-logout is **not** a JWT-style refresh token. It is TTL
management on the existing opaque session. "Refresh the session" = reset the Redis
key's expiry back to the idle window. The only backend gap is that nothing extends
the TTL today.

### Why the self-session endpoints must be token-scoped, not tenant-scoped

`/me`, `/logout`, and `/keepalive` all operate on the caller's **own** session
token, which already encodes `tenant_id` + `tenant_slug` (`SessionData`). Routing
them under `/tenants/{tenant_slug}/...` forces them through `tenant_guard`, which
(`auth/tenant_guard.py:59-79`) **403s any platform operator** (`tenant_id = None`)
unless they hold an active elevation grant into that specific tenant. Requiring an
elevation grant just to check who you are or to log out is wrong: these are
self-operations, unrelated to any tenant.

Per ADR-0006 §D, platform operators have no first-class HTTP login yet (GCIP is
deferred); today they exist only as directly-minted opaque sessions (the test
pattern). Building the self-session endpoints token-scoped makes them correct for
both tenant users now and platform sessions whenever they exist, with no
dependency on the deferred GCIP work.

Rejected alternative: giving platform operators a real "platform tenant" so the
null-tenant branch disappears. Rejected because it contradicts ADR-0006 and the
`app_user` CHECK constraint (`account_type='platform' ⇔ tenant_id IS NULL`),
doesn't actually remove the elevation/cross-tenant branching (which is what causes
most of the conditionals), and introduces a standing-privilege "super-tenant" —
the exact risk ADR-0006 was designed to avoid (privilege comes from RBAC +
audited elevation, never from tenancy). Changing the platform-identity model would
require an ADR-0006 amendment and is out of scope here.

## Design

### 1. Config — `packages/vera_core/src/vera_core/config/settings.py`

- Keep `session_ttl_seconds = 3600`. Its meaning becomes the **idle window** (the
  session dies this many seconds after the last extension). Compliance may lower it
  later; that is a separate decision and not part of this change.
- Add `session_absolute_max_seconds: int = 10 * 3600` (36000) — the **hard cap**.
  Value subject to compliance sign-off. This is the maximum total session lifetime
  regardless of activity.

### 2. Absolute cap via a companion Redis key — `auth/session.py`

The cap needs a notion of "born at T, die at T+max." We do **not** store a
wall-clock timestamp: the repo CLAUDE.md forbids the app clock as a time source
(`now()` is the DB clock), and a DB call on the auth hot path is unacceptable.
Instead we use a second Redis key whose own TTL is the absolute remaining time —
keeping everything on Redis's clock and matching the existing "Redis expiry *is*
the auto-logoff" design.

- New namespace constant: `SESSION_ABS_NS = "sess_abs"`.
- At login we mint **two** keys for one session:
  - `vera:sess:{token}` — TTL = idle window. Slid by keepalive.
  - `vera:sess_abs:{token}` — TTL = absolute max. **Never extended.**

New methods on the `SessionStore` protocol and both implementations
(`InMemorySessionStore`, `RedisSessionStore`):

- `mint_session(data: SessionData, idle_ttl: int, abs_ttl: int) -> str`
  - Generate a token; set the `sess` key (EX `idle_ttl`) and the `sess_abs` key
    (EX `abs_ttl`); return the token.
  - The `sess_abs` value is irrelevant (e.g. `"1"`); only its TTL matters.
- `extend_session(token: str, idle_ttl: int) -> int | None`
  - Read `abs_remaining` = TTL of `sess_abs`. If the key is gone or `<= 0`,
    return `None` (absolute cap reached / session gone).
  - Otherwise set the `sess` key's expiry to `min(idle_ttl, abs_remaining)` and
    return that new remaining-seconds value.
  - If the `sess` key no longer exists (idle-expired between verify and extend),
    return `None`.
- `delete_session(token: str) -> None`
  - DEL both keys. Used by logout.

`get` and `SessionVerifier.verify` are **unchanged** (single GET on `sess`).

Self-enforcing cap invariant: because every `extend_session` caps the `sess` TTL
to `abs_remaining`, and `abs_remaining` only decreases, the `sess` key can never
outlive the `sess_abs` key (`sess_ttl <= abs_remaining` always). Therefore the
absolute cap is enforced without any check on the verify hot path: once the
absolute window elapses, the `sess` key has already expired and `verify` 401s
normally.

### 3. Mint / teardown sites — `api/v1/auth.py`

- `login` (currently `store.put(SESSION_NS, replace(base, mfa_passed=True),
  settings.session_ttl_seconds)`) →
  `store.mint_session(replace(base, mfa_passed=True), settings.session_ttl_seconds,
  settings.session_absolute_max_seconds)`.
- `mfa_verify` — same swap (it mints the post-MFA session).
- `logout` — `store.delete(SESSION_NS, token)` → `store.delete_session(token)`.

### 4. Move the self-session trio to token-scoped `/auth/...`

Drop `tenant_guard` from these routes. Authentication is `Depends(current_identity)`
(validates the opaque session, rejects non-`mfa_passed`/expired tokens with 401),
plus `Depends(_bearer)` to obtain the raw token where a Redis op needs it.

| Route | New path | Handler shape |
|---|---|---|
| logout | `POST /auth/logout` | `current_identity` + `_bearer` → `store.delete_session(token)`; `ok(None, message="Logged out.")` |
| keepalive (new) | `POST /auth/session/keepalive` | `current_identity` + `_bearer` → `remaining = store.extend_session(token, settings.session_ttl_seconds)`; if `None` raise `UnauthorizedError`; else `ok(KeepaliveResponse(expires_in_seconds=remaining))`; set `Cache-Control: no-store` |
| me | `GET /auth/me` | `current_identity` + `self_scoped_session` (see §5); body otherwise unchanged |

New response model in `auth.py`:

```python
class KeepaliveResponse(BaseModel):
    expires_in_seconds: int
```

Route declarations follow the repo contract: `response_model=ResponseModel[T]` +
`responses=CustomAPIResponse.custom(DefaultExceptionCode.UNAUTHORIZED, ...)`.

No permission gate and no audit on keepalive — it is a self-operation on the
caller's own session and touches no PHI, matching `/me`.

`login`, `mfa/verify`, and `invitations/*` remain tenant-scoped (pre-auth,
slug-driven). This asymmetry is correct: slug for pre-auth, token for post-auth.

### 5. `self_scoped_session` dependency — `deps.py`

`/me` previously got its RLS scope from `tenant_guard`. After the move it must
choose its own scope from the verified identity:

```python
async def self_scoped_session(identity, sessionmaker) -> AsyncGenerator[AsyncSession]:
    if identity.tenant_id is not None:
        async with tenant_session(sessionmaker, identity.tenant_id) as session:
            yield session
    else:
        async with platform_session(sessionmaker) as session:
            yield session
```

`/me` then passes `identity.tenant_id` (possibly `None`) to
`resolver.effective_permissions(...)`.

Implementation verify-item: confirm `effective_permissions` accepts
`tenant_id=None` and that the role query resolves correctly under
`platform_session` (global NULL-tenant roles + the platform user's `user_role`
rows). The platform branch is correct-by-construction but is exercised only via
minted sessions until GCIP platform login ships (ADR-0006 §D).

## Behavioral contract

- A session lives at most `session_absolute_max_seconds` from login, and dies
  `session_ttl_seconds` after the most recent `keepalive` (or login), whichever is
  sooner.
- Normal authenticated requests do **not** slide the TTL. Only `keepalive` extends
  it. (This is the deliberate "explicit heartbeat" model: background/polling
  traffic cannot defeat the idle timeout.)
- `keepalive` returns the actual remaining seconds (`min(idle, abs_remaining)`), so
  the frontend can sync rather than assume.
- `keepalive` on an expired/absent session → 401.
- `logout` requires a valid session (`current_identity`); an already-expired token
  → 401 (same posture as today via `tenant_guard`), rather than silently tolerant.

## Testing

### Store unit tests (`InMemorySessionStore`; `RedisSessionStore` where integration available)

- `mint_session` creates both `sess` and `sess_abs` keys.
- `extend_session` caps the new TTL at `abs_remaining`: set `abs_ttl < idle_ttl`
  and assert the returned remaining equals the (smaller) absolute remaining — no
  `sleep`, avoiding the known flaky-timing trap.
- `extend_session` returns `None` once `sess_abs` is gone.
- `delete_session` removes both keys.

### Integration tests

- Update existing tests hitting `/tenants/{slug}/auth/{logout,me}` to the new
  `/auth/{logout,me}` paths.
- `keepalive`: login → keepalive returns `expires_in_seconds`; missing/expired
  token → 401; after logout → 401.

## Out of scope

- Frontend integration (idle timer, "Stay logged in?" prompt, multi-tab sync,
  401-redirect fallback). To be designed separately.
- Platform-operator HTTP login (GCIP token exchange) — ADR-0006 §D, deferred.
- Lowering `session_ttl_seconds` to a compliance-mandated idle window — separate
  compliance decision.
- Any change to the platform-identity model (null tenant vs platform tenant) —
  would require an ADR-0006 amendment.

## Files touched

- `packages/vera_core/src/vera_core/config/settings.py` — add `session_absolute_max_seconds`.
- `apps/control_plane/src/control_plane/auth/session.py` — `SESSION_ABS_NS`,
  `mint_session`, `extend_session`, `delete_session` on protocol + both impls;
  docstring update.
- `apps/control_plane/src/control_plane/api/v1/auth.py` — `KeepaliveResponse`;
  move `logout`/`me` to `/auth/...`; new `keepalive`; swap mint/teardown calls;
  module + route docstring updates.
- `apps/control_plane/src/control_plane/deps.py` — `self_scoped_session`.
- Tests: session store unit tests; `tests/integration/control_plane/test_login_flow.py`,
  `tests/integration/control_plane/test_admin.py` (path updates + keepalive cases).
</content>
</invoke>
