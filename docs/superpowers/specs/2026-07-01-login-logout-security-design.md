# Login/logout security: constant-time login + logout audit

**Date:** 2026-07-01
**Branch:** `fix/login-logout-security`
**Scope:** `vera-backend` control plane auth.

Two independent, small security fixes to the auth endpoints:

1. **Constant-time login** — close a user-enumeration timing side-channel in the
   password login path.
2. **Logout audit logging** — record `/auth/logout` in the auth audit trail, which
   currently logs login/MFA/invite events but nothing on logout.

---

## Part 1 — Constant-time login (kill the email-enumeration timing leak)

### Problem

In `apps/control_plane/src/control_plane/api/v1/auth.py`, the login failure check is:

```python
if (
    creds is None
    or creds.hashed_password is None
    or not verify_password(body.password, creds.hashed_password)
):
    ... raise _unauthorized()
```

All failures return the same uniform 401, but they take **different amounts of time**:

- **Unknown email** (`creds is None`) → Python short-circuits the `or`, so
  `verify_password()` never runs. bcrypt is deliberately slow (~100ms at cost 12);
  skipping it makes the response measurably faster.
- **Known email, wrong password** → the full bcrypt comparison runs → slower response.

An attacker scripting logins and measuring response time can distinguish "no such
account" (fast) from "real account, wrong password" (slow), leaking which emails are
registered. On a HIPAA clinical platform, "this person has an account" is itself
sensitive.

### Fix

Always perform an equivalent bcrypt comparison, even when the user isn't found, so
both branches do the same work and take the same time. This is the standard "dummy
verify" mitigation (Django auth, passlib).

**`auth/password.py`** — add the primitive where bcrypt already lives, so `auth.py`
stays simple and the mitigation is cohesive with the hashing code:

```python
# Generated once at import from the SAME _ROUNDS as real hashes, so an unknown-email
# verify costs exactly what a real verify costs (the cost factor drives bcrypt
# timing). A throwaway password — not a secret.
_DUMMY_HASH = hash_password("vera-timing-equalizer")


def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    """Constant-work verify: when there is no stored hash (unknown email, or a user
    with no password identity), still run a full bcrypt comparison against a dummy
    hash and return False. Keeps the unknown-email path the same latency as the
    wrong-password path (user-enumeration mitigation)."""
    return verify_password(password, hashed if hashed is not None else _DUMMY_HASH)
```

**Decision (import-time generation):** the dummy hash is produced at module import
from `_ROUNDS`, not pasted as a hardcoded literal, so if `_ROUNDS` ever changes the
dummy's cost factor tracks it automatically and the timing stays matched. Cost is one
extra bcrypt op at process startup.

**`api/v1/auth.py` login endpoint** — replace the short-circuiting failure check:

```python
password_ok = verify_password_or_dummy(
    body.password, creds.hashed_password if creds is not None else None
)
if creds is None or not password_ok:
    user_id = creds.user_id if creds is not None else None
    await _audit(audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=user_id)
    raise _unauthorized()
```

Import `verify_password_or_dummy` alongside the existing `verify_password` import.

Now every reachable failure branch runs exactly one full bcrypt op → identical timing:

- unknown email → dummy verify,
- user with `hashed_password is None` → dummy verify (folds into `not password_ok`),
- wrong password → real verify.

The `creds.hashed_password is None` sub-case no longer needs its own clause: the dummy
verify returns `False` for it, so `not password_ok` already covers it.

### Out of scope (explicit decision)

The unknown-tenant / no-password-provider path (the `if provider is None:` block, which
returns before `creds` is ever loaded) still returns fast without any bcrypt. That is a
**tenant-level** timing difference, not the **email-level** enumeration described here,
so it stays as-is. Noted as a known gap, not fixed in this change.

### Verification

- Behavior unchanged: all three failure modes still return the uniform 401; a correct
  password still succeeds.
- Timing: unknown-email and wrong-password responses now both incur one bcrypt op.
- Add/extend a unit test asserting `verify_password_or_dummy` returns `False` for a
  `None` hash **and** for a wrong password, and `True` for the right password against a
  real hash. (Timing itself isn't asserted in tests — it's inherently flaky; the
  guarantee is structural: both paths call bcrypt exactly once.)

---

## Part 2 — Logout audit logging

### Problem

The auth audit trail records login success/failure, MFA challenges, and invite events,
but `/auth/logout` writes **nothing**. There is no `LOGOUT` value in the `AuthEvent`
enum, so logouts are invisible in the compliance trail.

### Fix

Add a `LOGOUT` auth event, widen the DB CHECK constraint that enumerates auth events,
and emit the event from the logout endpoint.

**`models/enums.py`** — add to `AuthEvent`:

```python
LOGOUT = "logout"
```

**New Alembic migration** — `auth_audit_log.event_type` carries a CHECK constraint
(`ck_auth_audit_log_event_type_valid`) built from the `AuthEvent` enum, so an already
-provisioned database rejects `'logout'` until the CHECK is widened. Follow the exact
precedent in `migrations/versions/0017_persona_tweak_event.py`:

- Drop-and-recreate the named constraint from the **current** enum via `values_of(AuthEvent)`
  (`DROP CONSTRAINT IF EXISTS` → no-op on a fresh DB where migration 0001 already built it
  with `logout`; in-place widen on an existing DB).
- `downgrade()` recreates the constraint from the value set **without** `logout` (the
  current enum minus the new value).
- `down_revision = "0022"` (confirmed single head at time of writing).
- **Revision id / filename:** per `vera-backend/CLAUDE.md`, new migrations use a
  **random-hex revision id** with a **date-prefixed filename** — not a sequential
  `0023`. Scaffold with `just makemigration` to get the id + filename, then replace the
  autogenerated body with the CHECK-widen logic (Alembic autogenerate does not detect a
  CHECK-string change). Legacy `0001`–`0022` keep their sequential names; this one does
  not.

**`api/v1/auth.py` logout endpoint** — the verified identity is currently unused
(`_identity`) and neither `audit` nor `request` is injected. Change to:

```python
async def logout(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    request: Request,
    store: Store,
    audit: AuthAudit,
) -> ResponseModel[None]:
    # Token-scoped self-op: `current_identity` proves a live session (expired → 401);
    # the slug is irrelevant. delete_session reaps both the `sess` and `sess_abs` keys.
    if credentials is not None:
        await store.delete_session(credentials.credentials)
    await emit_auth_event(
        audit,
        tenant_id=identity.tenant_id,   # None for a platform operator → log_auth_event path
        event=AuthEvent.LOGOUT,
        ip=client_ip(request),
        user_id=identity.user_id,
    )
    return ok(None, message="Logged out.")
```

Key design points:

- **Live sessions only.** `current_identity` already gates the endpoint (expired → 401),
  so only real, live-session logouts are audited — exactly the action trail we want.
- **`emit_auth_event`, not the `_audit` helper.** `_audit` is typed `tenant_id: UUID`
  (non-null), but a platform operator's `tenant_id` is `None`. `emit_auth_event` accepts
  `UUID | None`, and the sink routes a null-tenant event through the `log_auth_event`
  SECURITY DEFINER function — the sanctioned platform write path (ADR-0006 §C). So logout
  audit works for both tenant users and platform operators.
- **Emit after `delete_session`.** The sink writes its own short transaction, so ordering
  is about intent: record the logout once the session is actually reaped.
- `emit_auth_event` and `client_ip` are already imported in this module.

### Verification

- Integration test: a logged-in tenant user hits `/auth/logout`; assert an
  `auth_audit_log` row with `event_type = 'logout'`, the caller's `app_user_id`, and the
  request IP; assert the session token is invalidated afterward.
- Migration round-trips: `just migrate` applies clean; the widened CHECK accepts
  `'logout'`; `downgrade` restores the prior constraint.

---

## Cross-cutting

- **Files touched:** `auth/password.py`, `api/v1/auth.py`, `models/enums.py`, one new
  migration, plus unit/integration tests.
- **No frontend change.** The frontend already calls `/auth/logout`; the audit write is
  server-side only.
- **Post-implementation (repo rule):** run `/simplify` on the change, then `just check`
  (ruff + mypy --strict + pytest) before committing.
