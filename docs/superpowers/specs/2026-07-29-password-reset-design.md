# Password Reset (forgot password from login page) — VR2-104

Date: 2026-07-29 · Branch: `feat/reset-password` · Scope: tenant users only

## Context

Vera has no self-service password reset: a tenant user who forgets their password is
stuck (only the admin-driven invite re-issue exists). This adds "Forgot password?" to
the login page: the user enters workspace + email, receives an emailed single-use
link (1 h TTL), sets a new password, every existing session is revoked, and they sign
in again. Auth today is hand-rolled bcrypt + opaque Redis sessions (no GCIP at
runtime); the invitation flow is the proven template — Redis token store with
sha256-hashed keys, SETEX TTL, single-use delete, `EmailSender` (sendria locally),
uniform 401s.

## Decisions

- Tenant users only. The platform tier needs a new SECURITY DEFINER update function —
  separate ticket.
- Confirm revokes ALL of the user's sessions (re-login required). No auto-login.
- MFA untouched: enrollment survives; next login does the normal MFA verify.
- Request endpoint rate limit: 3 per (tenant, email) / 15 min — silent generic 200
  when over limit (no probe surface).
- Token: 1 h TTL, single-use, `secrets.token_urlsafe(32)`, sha256-hashed at rest in
  Redis namespace `pwreset` (reusing `InvitationStore`/`InviteData`).
- Request endpoint always returns generic 200 regardless of unknown slug/email,
  deactivated or invited user (no enumeration); the email goes out as a detached
  task via the repo's dispatch pattern (`dispatch.schedule_detached` —
  `asyncio.create_task` + strong-ref tracking + shutdown/test drain), so response
  timing stays uniform.

## API surface (pre-auth, tenant-slug family, `Cache-Control: no-store`)

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/tenants/{slug}/auth/password-reset/request` | `{email}` | `ResponseModel[None]`, always 200 |
| GET | `/tenants/{slug}/auth/password-reset/validate?token=` | — | `{state: valid\|invalid\|deactivated}` |
| POST | `/tenants/{slug}/auth/password-reset/confirm` | `{token, password}` | 200; uniform 401; 400 >72-byte password |

Reset URL: `{frontend_base_url}/tenants/{slug}/reset-password?token=…` (invite
pattern, `users.py:179`).

## Implementation steps (one commit each, tests first)

### 1. Per-user session index + `delete_all_for_user`

`apps/control_plane/src/control_plane/auth/session.py`:
- `SESSION_USER_NS = "sess_user"` — Redis SET `vera:sess_user:{user_id}` of raw
  session tokens; protocol + both stores gain `delete_all_for_user(user_id)`.
- `RedisSessionStore.mint_session`: same MULTI adds `SADD` + `EXPIRE <abs_ttl>` on the
  index (refreshed each mint, so the index always outlives every member).
  `delete_session`: GET payload → DEL sess/sess_abs + SREM (skip SREM when the
  payload already expired — stale members are harmless, DEL is a no-op).
  `delete_all_for_user`: SMEMBERS → pipeline DEL every sess + sess_abs + the index.
- `InMemorySessionStore`: `_user_tokens: dict[UUID, set[str]]` mirror.
- Only `mint_session` indexes — mfa/mfa_enroll challenge tokens deliberately not.
- Tests: `tests/unit/auth/test_session.py` + `test_redis_backends.py` (fakeredis).

### 2. AuthEvents + CHECK-widening migration

- `vera_core/models/enums.py::AuthEvent` += `PASSWORD_RESET_REQUESTED`,
  `PASSWORD_RESET_COMPLETED`.
- Migration via `just makemigration`, copying the
  `20260728_2223_94f5bc060fac_widen_auth_audit_event_check_for_.py` pattern
  (drop + recreate `ck_auth_audit_log_event_type_valid` from `values_of(AuthEvent)`;
  downgrade subtracts the two new values). Random-hex revision id.

### 3. Settings, token namespace, rate limiter, wiring

- `vera_core/config/settings.py`: `password_reset_ttl_seconds=3600`,
  `password_reset_rate_limit=3`, `password_reset_rate_limit_window_seconds=900`.
- `auth/invitations.py`: `PASSWORD_RESET_NS = "pwreset"`.
- `rate_limit.py`: string-keyed `PasswordResetRateLimiter` protocol + InMemory/Redis
  impls (copy `RedisCallRateLimiter`'s INCR + EXPIRE-NX MULTI shape; prefix
  `vera:pwreset-rl:`). Call-site key = `sha256(f"{slug}:{email.lower()}")` — raw email
  never lands in a Redis key; unknown emails increment too.
- `main.py::create_app` kwarg + lifespan wiring; `deps.py::get_password_reset_rate_limiter`;
  integration conftest injects the in-memory limiter. Unit tests beside the
  coaching-limiter tests.

### 4. Endpoints + email helper (in `api/v1/auth.py`)

- Never-raising `_send_password_reset_email(...) -> bool` (template
  `platform_users.py:71-102`); never log the token or URL.
- **request**: no-store → rate-limit (silent drop, audit `reason="rate_limited"`) →
  `resolve_tenant_id` (None ⇒ silent 200) → `_load_password_creds(email,
  account_type="tenant")` (plane-pinned, auth.py:244) → eligible iff
  `status == "active"` and `hashed_password` present (quietly covers unknown /
  invited / deactivated / identity-less) → mint token + detached-task email +
  audit REQUESTED → single `ok(None)` return for every branch.
- **validate**: mirror `validate_invitation` (auth.py:745) — gather slug + token,
  mismatch ⇒ `invalid`; `AppUser.status` → valid/deactivated/invalid. Not consumed.
- **confirm**: uniform `_unauthorized()` on token/tenant issues; 400 over-length
  (mirror auth.py:809); AppUser must be active;
  `_password_identity_row(for_update=True)` (auth.py:729);
  `hashed_password = hash_password(...)` ONLY (MFA fields untouched); after commit:
  `invites.delete` (single-use) → `store.delete_all_for_user` → audit COMPLETED.
- Integration tests `tests/integration/control_plane/test_password_reset.py`
  (clone the `login_world` fixture; extract token from the sent email body via
  `split("token=")` as in `test_admin.py`):
  happy path (old password 401 / new password 200); a held session 401s after reset;
  uniform 200 + zero emails for unknown slug/email/deactivated/invited; single-use;
  tenant-mismatch leaves the token unconsumed; over-length 400; rate limit still
  returns 200 with no extra email; MFA user still gets `mfa:"verify"` next login;
  audit rows carry no token.

### 5. Frontend API functions

`src/lib/auth/api.ts` next to the invitation block: `requestPasswordReset`,
`validatePasswordReset`, `confirmPasswordReset` — all `auth:false`, `tenantAuth(slug)`
builder, reuse `InviteValidateResult`. Extend `src/lib/auth/api.test.ts`.

### 6. Frontend pages, routes, login link

- New `src/pages/ForgotPassword.tsx` (route `/forgot-password`): Workspace field
  (prefill `VITE_DEFAULT_TENANT_SLUG`) + Email with `emailError` + touched gating
  (copy `Login.tsx`); terminal card "If an account exists for that email, we've sent a
  reset link — it expires in 1 hour" on any 2xx; inline `role="alert"` errors only for
  network/5xx; footer link back to `/login`.
- New `src/pages/ResetPassword.tsx` (route `/tenants/:tenantSlug/reset-password?token=`):
  structural copy of `AcceptInvite.tsx` minus the MFA phases —
  `Phase = checking|invalid|deactivated|password|done`; validate on mount
  (cancelled-flag effect); `PasswordInput minLength={8} autoComplete="new-password"`;
  done card → `navigate("/login", { replace: true })`.
- `src/App.tsx`: two routes in the pre-auth block. `src/pages/Login.tsx`: footer
  `<Link to="/forgot-password">` (copy `PlatformLogin.tsx:130-134` classes).
- Tests: `ResetPassword.test.tsx` (no-token → invalid card; renderToStaticMarkup +
  MemoryRouter per `PlatformAcceptInvite.test.tsx`), `ForgotPassword.test.tsx`,
  login-link assertion.

## Verification

- Backend: `just check` verbatim; `just up && just migrate` proves the migration;
  `/simplify` then re-run `just check`.
- Frontend: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build`.
- E2E manual: dev servers + sendria (http://localhost:1080) — login → Forgot
  password → email link → new password → old session dead → re-login (+ MFA verify
  when enrolled).

## Accepted risks (note in PR body)

- Sessions minted before this release have no index entry, so a reset in the first
  hours post-deploy won't revoke them (bounded by 15-min idle / 10-h absolute TTL).
- ~ms race between a concurrent old-password login and the revoke snapshot — accepted.
- SMTP failure is invisible to the requester (detached send); recovery = request again.
- `invited` users get silence on request (no invite auto-resend) — product follow-up
  if wanted.
