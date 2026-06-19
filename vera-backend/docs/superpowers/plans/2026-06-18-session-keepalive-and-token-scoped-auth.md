# Session keepalive + token-scoped self-session endpoints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add idle auto-logout (sliding TTL) with a hard absolute session cap to the opaque Redis session, and move `/me`, `/logout`, and a new `/keepalive` onto a token-authenticated `/auth/...` surface that works for tenant users and platform operators alike.

**Architecture:** A session is two Redis keys — `vera:sess:{token}` (TTL = idle window, slid by keepalive) and `vera:sess_abs:{token}` (TTL = absolute max, never extended). Every extension is capped at the abs key's remaining TTL, so the sess key can never outlive the cap; the verify hot path stays a single GET and no wall clock is ever read. The three self-session endpoints drop `tenant_guard` and authenticate purely from the opaque token (which already carries `tenant_id`/`tenant_slug`).

**Tech Stack:** Python 3.12, FastAPI, `redis.asyncio`, SQLAlchemy async, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

## Global Constraints

- Python pinned 3.12 (`<3.13`); PEP 695 type params only (`class Foo[T]`) — ruff rejects `Generic[T]`/`TypeVar`.
- Single async runtime is `asyncio`; never `import anyio`.
- `just check` (ruff + mypy --strict + pytest) is the CI gate; it must pass before any task is "done".
- Tests are plain `async def` (no `@pytest.mark.asyncio`) — `asyncio_mode = "auto"`.
- No wall-clock time source in app code: never `datetime.now()`; DB `now()`/Redis TTLs only. (This plan reads no clock at all.)
- Endpoints return `ResponseModel[T]` via `ok(...)`; errors `raise` `CustomAPIException` subclasses; never `HTTPException` in route bodies (deps may).
- No DB migration in this work — config + Redis + routing only.
- Commit messages: do **not** add a `Co-Authored-By` trailer (user global rule).
- After implementation, run the `/simplify` skill on the change, then re-run `just check` (repo root CLAUDE.md).

---

### Task 1: SessionStore companion-key methods

Add the two-key session primitives to the store. Pure unit-testable change; nothing else depends on it yet.

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/session.py`
- Test: `tests/unit/auth/test_session.py` (create)

**Interfaces:**
- Consumes: existing `SessionData`, `_key`, `_new_token`, `SESSION_NS` from `auth/session.py`.
- Produces (relied on by Tasks 2, 4, 5):
  - `SESSION_ABS_NS: str = "sess_abs"`
  - `SessionStore.mint_session(data: SessionData, idle_ttl: int, abs_ttl: int) -> str`
  - `SessionStore.extend_session(token: str, idle_ttl: int) -> int | None`
  - `SessionStore.delete_session(token: str) -> None`
  - Same three methods on `InMemorySessionStore` and `RedisSessionStore`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/auth/test_session.py`:

```python
"""Unit tests for the two-key session model: a `sess` key (idle TTL, slid by
keepalive) plus a `sess_abs` companion key (absolute cap, never extended). Every
extension is capped at the abs key's remaining TTL, so the session can never
outlive the cap."""

from uuid import uuid4

from control_plane.auth.session import (
    SESSION_ABS_NS,
    SESSION_NS,
    InMemorySessionStore,
    SessionData,
    _key,
)


def _data() -> SessionData:
    return SessionData(
        user_id=uuid4(),
        tenant_id=uuid4(),
        email="u@example.com",
        subject="u@example.com",
        provider_type="password",
        mfa_passed=True,
    )


async def test_mint_session_sets_both_keys() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    assert _key(SESSION_NS, token) in store._entries
    assert _key(SESSION_ABS_NS, token) in store._entries
    assert (await store.get(SESSION_NS, token)) is not None


async def test_extend_session_returns_idle_when_below_cap() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=1000)
    remaining = await store.extend_session(token, idle_ttl=10)
    assert remaining is not None
    assert 9 <= remaining <= 10


async def test_extend_session_caps_at_absolute_remaining() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=1000, abs_ttl=50)
    remaining = await store.extend_session(token, idle_ttl=1000)
    assert remaining is not None
    assert remaining < 1000          # capped below the idle window
    assert 49 <= remaining <= 50     # ~= absolute remaining


async def test_extend_session_none_when_absolute_expired() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    store._entries.pop(_key(SESSION_ABS_NS, token))  # simulate the abs key reaped
    assert (await store.extend_session(token, idle_ttl=10)) is None


async def test_delete_session_removes_both_keys() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    await store.delete_session(token)
    assert _key(SESSION_NS, token) not in store._entries
    assert _key(SESSION_ABS_NS, token) not in store._entries
    assert (await store.extend_session(token, idle_ttl=10)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/auth/test_session.py` (or `uv run pytest tests/unit/auth/test_session.py -v`)
Expected: FAIL / ERROR — `ImportError: cannot import name 'SESSION_ABS_NS'` and `AttributeError: 'InMemorySessionStore' object has no attribute 'mint_session'`.

- [ ] **Step 3: Add the namespace constant**

In `apps/control_plane/src/control_plane/auth/session.py`, next to `SESSION_NS`/`MFA_NS` (currently lines 28-29):

```python
SESSION_NS = "sess"
MFA_NS = "mfa"
# Companion key for a fully-authenticated session: same token, holds no data — only
# its TTL, set once at login to the absolute max and NEVER extended. extend_session
# caps the sliding `sess` TTL at this key's remaining TTL, so `sess` can never outlive
# the cap and the verify hot path needs no clock and no extra read.
SESSION_ABS_NS = "sess_abs"
```

- [ ] **Step 4: Add methods to the `SessionStore` Protocol**

In `session.py`, extend the `SessionStore` Protocol (currently has `put`/`get`/`delete`, lines 88-94):

```python
class SessionStore(Protocol):
    async def put(self, namespace: str, data: SessionData, ttl_seconds: int) -> str:
        """Store `data` under a fresh opaque token and return the token."""
        ...

    async def get(self, namespace: str, token: str) -> SessionData | None: ...
    async def delete(self, namespace: str, token: str) -> None: ...

    async def mint_session(self, data: SessionData, idle_ttl: int, abs_ttl: int) -> str:
        """Mint a fully-authenticated session: a `sess` key (EX idle_ttl) plus a
        `sess_abs` companion (EX abs_ttl). Returns the shared opaque token."""
        ...

    async def extend_session(self, token: str, idle_ttl: int) -> int | None:
        """Slide the `sess` TTL to min(idle_ttl, absolute remaining). Returns the new
        remaining seconds, or None if the absolute cap is reached or the session is gone."""
        ...

    async def delete_session(self, token: str) -> None:
        """Delete both the `sess` and `sess_abs` keys (logout)."""
        ...
```

- [ ] **Step 5: Implement on `InMemorySessionStore`**

In `InMemorySessionStore` (the dev/test store; entries are `dict[str, tuple[float, SessionData]]` keyed by `_key(...)`, using `time.monotonic()`), add after the existing `delete` method:

```python
    async def mint_session(self, data: SessionData, idle_ttl: int, abs_ttl: int) -> str:
        token = _new_token()
        now = time.monotonic()
        self._entries[_key(SESSION_NS, token)] = (now + idle_ttl, data)
        self._entries[_key(SESSION_ABS_NS, token)] = (now + abs_ttl, data)
        return token

    async def extend_session(self, token: str, idle_ttl: int) -> int | None:
        now = time.monotonic()
        abs_entry = self._entries.get(_key(SESSION_ABS_NS, token))
        if abs_entry is None:
            return None
        abs_expires_at, _ = abs_entry
        abs_remaining = abs_expires_at - now
        if abs_remaining <= 0:
            return None
        sess_key = _key(SESSION_NS, token)
        sess_entry = self._entries.get(sess_key)
        if sess_entry is None:
            return None
        _, data = sess_entry
        new_ttl = min(idle_ttl, int(abs_remaining))
        self._entries[sess_key] = (now + new_ttl, data)
        return new_ttl

    async def delete_session(self, token: str) -> None:
        self._entries.pop(_key(SESSION_NS, token), None)
        self._entries.pop(_key(SESSION_ABS_NS, token), None)
```

- [ ] **Step 6: Implement on `RedisSessionStore`**

In `RedisSessionStore` (uses `self._redis`, `SET ... ex=` / `DEL`), add after the existing `delete` method:

```python
    async def mint_session(self, data: SessionData, idle_ttl: int, abs_ttl: int) -> str:
        token = _new_token()
        await self._redis.set(_key(SESSION_NS, token), data.to_json(), ex=idle_ttl)
        # The companion value is irrelevant — only its TTL matters.
        await self._redis.set(_key(SESSION_ABS_NS, token), "1", ex=abs_ttl)
        return token

    async def extend_session(self, token: str, idle_ttl: int) -> int | None:
        # TTL: positive seconds remaining; -1 (no expiry, never happens here) / -2 (no key).
        abs_remaining = await self._redis.ttl(_key(SESSION_ABS_NS, token))
        if abs_remaining <= 0:
            return None
        new_ttl = min(idle_ttl, abs_remaining)
        extended = await self._redis.expire(_key(SESSION_NS, token), new_ttl)
        if not extended:  # sess key already gone (idle-expired)
            return None
        return new_ttl

    async def delete_session(self, token: str) -> None:
        await self._redis.delete(_key(SESSION_NS, token), _key(SESSION_ABS_NS, token))
```

- [ ] **Step 7: Update the module docstring**

In `session.py`, extend the top-of-file docstring's keyspace list to mention the companion key, e.g. add a bullet:

```
  * "sess_abs" — a companion to a "sess" token holding only the absolute-cap TTL,
                 set once at login and never extended; bounds total session lifetime.
```

- [ ] **Step 8: Run the tests and type/lint checks**

Run: `just test tests/unit/auth/test_session.py`
Expected: PASS (5 passed).
Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/session.py tests/unit/auth/test_session.py
git commit -m "feat(auth): two-key session store — sliding idle TTL + absolute cap"
```

---

### Task 2: Config + wire login/mfa_verify to mint_session

Add the absolute-cap setting and route the two mint sites through `mint_session`. Verified by the existing live login tests (the app's in-memory store now mints two keys; login must still work).

**Files:**
- Modify: `packages/vera_core/src/vera_core/config/settings.py:40-41`
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py` (login mint ~283-285; mfa_verify mint ~333-335)

**Interfaces:**
- Consumes: `SessionStore.mint_session` (Task 1); `settings.session_ttl_seconds` (existing).
- Produces: `Settings.session_absolute_max_seconds: int`; both mint sites now create the companion key.

- [ ] **Step 1: Add the setting**

In `settings.py`, immediately after `session_ttl_seconds: int = 3600` / `mfa_challenge_ttl_seconds: int = 300` (lines 40-41), add:

```python
    # Hard ceiling on total session lifetime regardless of activity. `session_ttl_seconds`
    # is the idle window (slid by /auth/session/keepalive); this is the absolute max set
    # once at login and never extended. Subject to compliance sign-off.
    session_absolute_max_seconds: int = 10 * 3600
```

- [ ] **Step 2: Point the login mint at mint_session**

In `auth.py` `login`, replace the post-password mint (currently lines 283-285):

```python
    token = await store.put(
        SESSION_NS, replace(base, mfa_passed=True), settings.session_ttl_seconds
    )
```

with:

```python
    token = await store.mint_session(
        replace(base, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
```

- [ ] **Step 3: Point the mfa_verify mint at mint_session**

In `auth.py` `mfa_verify`, replace the post-MFA mint (currently lines 333-335):

```python
    token = await store.put(
        SESSION_NS, replace(challenge, mfa_passed=True), settings.session_ttl_seconds
    )
```

with:

```python
    token = await store.mint_session(
        replace(challenge, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
```

> Note: leave the `store.put(MFA_NS, ...)` challenge mint in `login` untouched — the MFA challenge is single-key and not absolute-capped.

- [ ] **Step 4: Run the live login tests**

Run: `just test tests/integration/control_plane/test_login_flow.py` (requires `just up` + `just migrate`)
Expected: PASS — login and MFA verify still return a `session_token`, proving `mint_session` is wired and the in-memory store mints both keys without breaking login. (`/me` tests in this file still target the old tenant-scoped path; they're updated in Task 3.)

- [ ] **Step 5: Type/lint + commit**

Run: `just lint && just typecheck`
Expected: clean.

```bash
git add packages/vera_core/src/vera_core/config/settings.py apps/control_plane/src/control_plane/api/v1/auth.py
git commit -m "feat(auth): mint sessions with an absolute-cap companion key"
```

---

### Task 3: Token-scope `/me` (self-scoped session)

Move `/me` off the tenant-slug path; pick its RLS scope from the verified identity so it works for tenant users and platform operators.

**Files:**
- Modify: `apps/control_plane/src/control_plane/deps.py` (add `self_scoped_session`; imports already present)
- Modify: `apps/control_plane/src/control_plane/api/v1/common.py:44-49` (add `SelfScopedSession` alias)
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py` (`get_me` route + signature + import)
- Modify: `tests/integration/control_plane/test_login_flow.py` (`/me` paths; delete obsolete test)
- Modify: `tests/integration/control_plane/test_admin.py:36,49` (`/me` paths)

**Interfaces:**
- Consumes: `current_identity`, `tenant_session`, `platform_session` (all already imported in `deps.py`); `PermissionResolver.effective_permissions(session, tenant_id: UUID | None, user_id)` (already accepts `None`).
- Produces: `deps.self_scoped_session` async-generator dep; `common.SelfScopedSession = Annotated[AsyncSession, Depends(self_scoped_session)]`.

- [ ] **Step 1: Update the `/me` integration tests to the new path (failing)**

In `tests/integration/control_plane/test_login_flow.py`:
- `test_me_hydrates_session` (line 154): change
  `await client.get(f"{_base(world)}/auth/me", ...)` →
  `await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})`
- `test_me_requires_authentication` (line 171): change
  `await client.get(f"{_base(world)}/auth/me")` → `await client.get("/api/v1/auth/me")`
- **Delete** `test_me_rejects_other_tenant_slug` entirely (lines 175-189) — `/me` is no longer tenant-scoped, so a cross-tenant-slug URL no longer exists. (Tenant-mismatch protection is still covered for genuinely tenant-scoped routes elsewhere, e.g. the `/calls` path test in this file.)

In `tests/integration/control_plane/test_admin.py`:
- `test_me_self_read_allowed_without_any_permission` (line 36) and `test_me_lists_admin_roles_and_permissions` (line 49): change both
  `f"/api/v1/tenants/{tid}/auth/me"` → `"/api/v1/auth/me"`. (The `tid` local can stay; it's just unused for the path now — or remove it if ruff flags it.)

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/integration/control_plane/test_login_flow.py::test_me_hydrates_session tests/integration/control_plane/test_admin.py::test_me_self_read_allowed_without_any_permission`
Expected: FAIL — `404` for `/api/v1/auth/me` (route not yet defined).

- [ ] **Step 3: Add `self_scoped_session` to `deps.py`**

Append to `apps/control_plane/src/control_plane/deps.py` (after `platform_scoped_session`, end of file). All names used are already imported:

```python
async def self_scoped_session(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AsyncGenerator[AsyncSession]:
    """RLS scope for a caller reading their OWN data (no tenant in the URL): a tenant
    user pins their own verified tenant; a platform operator (`tenant_id is None`) gets
    the no-GUC platform session that resolves global/SUPER_ADMIN rows. No elevation,
    no slug — the scope comes from the verified identity, not request input."""
    if identity.tenant_id is not None:
        async with tenant_session(sessionmaker, identity.tenant_id) as session:
            yield session
    else:
        async with platform_session(sessionmaker) as session:
            yield session
```

- [ ] **Step 4: Add the `SelfScopedSession` alias to `common.py`**

In `apps/control_plane/src/control_plane/api/v1/common.py`, add `self_scoped_session` to the `control_plane.deps` import (lines 19-25) and a new alias next to the others (after line 44):

```python
from control_plane.deps import (
    get_auth_audit,
    get_email_sender,
    get_invitation_store,
    get_settings_state,
    self_scoped_session,
    tenant_scoped_session,
)
```

```python
TenantSession = Annotated[AsyncSession, Depends(tenant_scoped_session)]
SelfScopedSession = Annotated[AsyncSession, Depends(self_scoped_session)]
```

- [ ] **Step 5: Rewrite the `/me` route**

In `auth.py`, add `SelfScopedSession` to the `control_plane.api.v1.common` import block (lines 27-34, alphabetical with `Resolver`/`TenantSession`). Then change the `get_me` decorator + signature + the `effective_permissions` call. Replace the decorator path and the `tenant_id`/`session` params:

Decorator (currently `"/tenants/{tenant_slug}/auth/me"`):

```python
@router.get(
    "/auth/me",
    response_model=ResponseModel[MeResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_me(
    response: Response,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    session: SelfScopedSession,
    resolver: Resolver,
) -> ResponseModel[MeResponse]:
```

(Remove the `tenant_id: Annotated[UUID, Depends(tenant_guard)]` parameter.)

Then change the permissions call from `tenant_id` to `identity.tenant_id`:

```python
    resolved_id, permissions = await resolver.effective_permissions(
        session, identity.tenant_id, identity.user_id
    )
```

The rest of `get_me` (the `AppUser.name`/`account_type` query, the roles query, the `MeResponse(...)` construction using `identity.tenant_id`/`identity.tenant_slug`) is unchanged.

- [ ] **Step 6: Update docstrings**

In `get_me`'s docstring, drop the "Deliberately NO permission gate … tenant-scoped" wording that references the slug guard; note it is token-scoped and resolves scope from the identity (tenant or platform). One or two sentences.

- [ ] **Step 7: Run the `/me` tests**

Run: `just test tests/integration/control_plane/test_login_flow.py tests/integration/control_plane/test_admin.py`
Expected: PASS — `/me` works at `/api/v1/auth/me`, `Cache-Control: no-store` present, roles/permissions populated, unauth → 401. The deleted cross-tenant test no longer runs.

- [ ] **Step 8: Type/lint + commit**

Run: `just lint && just typecheck`
Expected: clean. (If ruff flags an unused `tid`/`UUID`/`tenant_guard`, remove only what is genuinely unused — `tenant_guard` is still used by `mfa_enroll`/`mfa_activate`, so it stays.)

```bash
git add apps/control_plane/src/control_plane/deps.py apps/control_plane/src/control_plane/api/v1/common.py apps/control_plane/src/control_plane/api/v1/auth.py tests/integration/control_plane/test_login_flow.py tests/integration/control_plane/test_admin.py
git commit -m "feat(auth): token-scope /me as /auth/me (self-scoped session)"
```

---

### Task 4: Token-scope `/logout`

Move `/logout` to `/auth/logout`, drop `tenant_guard`, and use `delete_session` so both keys are reaped.

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py` (`logout` route + body)
- Test: `tests/integration/control_plane/test_login_flow.py` (add a logout test)

**Interfaces:**
- Consumes: `current_identity`, `_bearer`, `SessionStore.delete_session` (Task 1), `Store` alias (existing).
- Produces: `POST /api/v1/auth/logout`.

- [ ] **Step 1: Write the failing logout test**

Append to `tests/integration/control_plane/test_login_flow.py`:

```python
async def test_logout_invalidates_session(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200
    assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 200
    # Session is gone — the same token no longer authenticates.
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/integration/control_plane/test_login_flow.py::test_logout_invalidates_session`
Expected: FAIL — `404` on `POST /api/v1/auth/logout` (route still at the tenant-scoped path).

- [ ] **Step 3: Rewrite the `logout` route**

In `auth.py`, replace the current `logout` decorator + function (currently `"/tenants/{tenant_slug}/auth/logout"`, params `tenant_id=Depends(tenant_guard)`, `credentials`, `store`) with:

```python
@router.post(
    "/auth/logout",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
    ),
)
async def logout(
    _identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Store,
) -> ResponseModel[None]:
    # Token-scoped self-op: `current_identity` proves a live session (expired → 401);
    # the slug is irrelevant. delete_session reaps both the `sess` and `sess_abs` keys.
    if credentials is not None:
        await store.delete_session(credentials.credentials)
    return ok(None, message="Logged out.")
```

- [ ] **Step 4: Run the test**

Run: `just test tests/integration/control_plane/test_login_flow.py::test_logout_invalidates_session`
Expected: PASS.

- [ ] **Step 5: Type/lint + commit**

Run: `just lint && just typecheck`
Expected: clean.

```bash
git add apps/control_plane/src/control_plane/api/v1/auth.py tests/integration/control_plane/test_login_flow.py
git commit -m "feat(auth): token-scope /logout as /auth/logout; reap both session keys"
```

---

### Task 5: Keepalive endpoint + final docs/check

Add `POST /auth/session/keepalive`, the `KeepaliveResponse` model, integration tests, and finish the module docstring + full gate.

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py` (model + route + module docstring)
- Test: `tests/integration/control_plane/test_login_flow.py` (keepalive tests)

**Interfaces:**
- Consumes: `current_identity`, `_bearer`, `SessionStore.extend_session` (Task 1), `Store`, `AppSettings` (existing aliases), `UnauthorizedError`, `Response`.
- Produces: `KeepaliveResponse(expires_in_seconds: int)`; `POST /api/v1/auth/session/keepalive`.

- [ ] **Step 1: Write the failing keepalive tests**

Append to `tests/integration/control_plane/test_login_flow.py`:

```python
async def test_keepalive_extends_session(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]

    resp = await client.post(
        "/api/v1/auth/session/keepalive", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["Cache-Control"] == "no-store"
    remaining = resp.json()["data"]["expires_in_seconds"]
    assert isinstance(remaining, int)
    assert 0 < remaining <= 3600  # within the default idle window


async def test_keepalive_requires_authentication(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, _world = login_world
    resp = await client.post("/api/v1/auth/session/keepalive")
    assert resp.status_code == 401


async def test_keepalive_after_logout_is_401(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]
    auth = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/logout", headers=auth)
    resp = await client.post("/api/v1/auth/session/keepalive", headers=auth)
    assert resp.status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/integration/control_plane/test_login_flow.py::test_keepalive_extends_session`
Expected: FAIL — `404` (route not defined).

- [ ] **Step 3: Add the response model**

In `auth.py`, near the other response models (after `SessionResponse`, ~line 93), add:

```python
class KeepaliveResponse(BaseModel):
    expires_in_seconds: int
```

- [ ] **Step 4: Add the keepalive route**

In `auth.py`, add (logically next to `logout`):

```python
@router.post(
    "/auth/session/keepalive",
    response_model=ResponseModel[KeepaliveResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
    ),
)
async def keepalive(
    response: Response,
    _identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Store,
    settings: AppSettings,
) -> ResponseModel[KeepaliveResponse]:
    """Slide the caller's own session by the idle window (capped at the absolute max).
    Token-scoped self-op, no tenant guard, no PHI, no audit. Returns the new remaining
    seconds so the client can sync its idle timer. `Cache-Control: no-store`."""
    response.headers["Cache-Control"] = "no-store"
    remaining = (
        await store.extend_session(credentials.credentials, settings.session_ttl_seconds)
        if credentials is not None
        else None
    )
    if remaining is None:
        raise UnauthorizedError(message="session expired")
    return ok(KeepaliveResponse(expires_in_seconds=remaining))
```

- [ ] **Step 5: Update the module docstring**

In `auth.py`'s top-of-file docstring, add a short line noting that `/auth/me`, `/auth/logout`, and `/auth/session/keepalive` are token-scoped self-session endpoints (no tenant slug, no `tenant_guard`), in contrast to the tenant-slug pre-auth routes (`login`, `mfa/verify`, `invitations/*`).

- [ ] **Step 6: Run the keepalive tests**

Run: `just test tests/integration/control_plane/test_login_flow.py`
Expected: PASS (all login/me/logout/keepalive tests green).

- [ ] **Step 7: Run `/simplify` then the full gate**

Run the `/simplify` skill on the working-tree changes (reuse/simplification/altitude only), then:

Run: `just check`
Expected: ruff + mypy --strict + full pytest all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/auth.py tests/integration/control_plane/test_login_flow.py
git commit -m "feat(auth): add /auth/session/keepalive (sliding idle TTL, capped)"
```

---

## Self-Review

**Spec coverage:**
- §1 config (`session_absolute_max_seconds = 10h`) → Task 2 Step 1. ✓
- §2 companion-key store (`mint_session`/`extend_session`/`delete_session`, `SESSION_ABS_NS`, unchanged `get`/verify) → Task 1. ✓
- §3 mint/teardown swaps (login, mfa_verify → mint; logout → delete_session) → Task 2 (mints) + Task 4 (logout teardown). ✓
- §4 token-scoped routes (logout, keepalive, me) → Tasks 3/4/5. ✓
- §5 `self_scoped_session` + alias; `effective_permissions` accepts `None` (confirmed in `rbac.py:52`, caveat dropped) → Task 3. ✓
- §6 login/mfa/invitations stay tenant-scoped → untouched (no task), correct. ✓
- §7 tests (store unit; integration path updates + keepalive) → Tasks 1, 3, 4, 5. ✓
- §8 doc updates (session.py, auth.py docstrings) → Task 1 Step 7, Task 3 Step 6, Task 5 Step 5. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — every code step shows full code. ✓

**Type consistency:** `mint_session(data, idle_ttl, abs_ttl) -> str`, `extend_session(token, idle_ttl) -> int | None`, `delete_session(token) -> None` are identical across the Protocol, both impls, and all call sites. `SelfScopedSession`/`self_scoped_session` names match between `deps.py`, `common.py`, and `auth.py`. `KeepaliveResponse.expires_in_seconds: int` matches the test assertion. ✓
</content>
</invoke>
</invoke>
