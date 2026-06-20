# Frontend Auth Integration (Redux Toolkit) — Design

**Date:** 2026-06-19
**Status:** Approved (design); pending implementation plan
**Target:** `vera-frontend` (React 19 + TS + Vite + react-router v7 + Tailwind + shadcn/ui)

## Goal

Integrate the Vera 2.0 backend authentication flow into the frontend using **Redux
Toolkit** for auth state, building the required login / MFA / invite screens. Auth is
multi-tenant, opaque Redis-session based (not JWT), with TOTP MFA and RBAC. All endpoints
are prefixed with `/api/v1`.

## Decisions (from brainstorming)

1. **Introduce Redux Toolkit** (`@reduxjs/toolkit` + `react-redux`). Migrate the existing
   React Context auth (`auth-context.tsx`) to Redux. Context provider is removed.
2. **`createAsyncThunk` + `createSlice`** (not RTK Query) — auth is a stateful step machine.
3. **Tenant slug in the route path**, under `/tenants/:tenantSlug/...`, so backend-generated
   invite links resolve directly.

## Exact backend contract (verified against source)

Tenant-scoped (slug in URL), unauthenticated:
- `POST /tenants/{slug}/auth/login` — body `{ email, password }` →
  `{ mfa: "none"|"verify"|"enroll", session_token?, mfa_token?, provisioning_uri? }`
- `POST /tenants/{slug}/auth/mfa/verify` — body `{ mfa_token, code }` → `{ session_token }`
- `POST /tenants/{slug}/auth/mfa/enroll-activate` — body `{ mfa_token, code }` →
  `{ session_token, recovery_codes[] }`
- `POST /tenants/{slug}/auth/invitations/accept` — body `{ token, password }` →
  `{ mfa_required, provisioning_uri?, mfa_token? }`
- `POST /tenants/{slug}/auth/invitations/activate-mfa` — body `{ mfa_token, code }` →
  `{ recovery_codes[] }`  (no session — user logs in afterward)

Token-scoped (NO slug in URL), Bearer-authenticated:
- `GET  /auth/me` → `{ user_id, email, name, account_type, tenant_id, tenant_slug, roles[], permissions[] }`
- `POST /auth/logout` → `null`
- `POST /auth/session/keepalive` → `{ expires_in_seconds }`
- `POST /auth/mfa/enroll` → `{ provisioning_uri }`
- `POST /auth/mfa/activate` — body `{ code }` → `{ recovery_codes[] }`

Admin user management (Bearer + RBAC):
- `POST /users/invitations` — permission **`users:manage`**, requires an **`Idempotency-Key`**
  header. Body `{ email, name, role_ids[], send_email }` → `{ user_id, email, invite_url, email_sent }`.
- Invite link format the backend builds: `{app_base_url}/tenants/{slug}/accept-invite?token={token}`.

All responses ride the envelope `{ data, status, message, error_code, description }` already
handled by `src/lib/api/client.ts` → `ApiError`.

> NOTE: the current `src/lib/auth/api.ts` is written against a wrong/older contract
> (`mfa_required` / `challenge_token`, and calls `/me` & `/logout` *with* a slug). It will be
> rewritten to the contract above.

## Architecture

### Store
- `src/store/index.ts` — `configureStore({ reducer: { auth } })`, exported `RootState` / `AppDispatch`.
- `src/store/hooks.ts` — typed `useAppDispatch` / `useAppSelector`.
- `<Provider store={store}>` wraps the router in `App.tsx` (replaces `AuthProvider`).

### Auth slice — `src/store/authSlice.ts`
State:
```
status: "loading" | "anonymous" | "authenticated"
user: MeResponse | null            // includes roles[], permissions[]
tenantSlug: string | null
mfa: { token: string; step: "verify" | "enroll"; provisioningUri?: string } | null
loading: boolean
error: string | null
```
- `mfa_token` lives ONLY in `mfa.token`; cleared the instant a `session_token` is issued.
- `forceLogout` reducer: clears state + sessionStorage, sets `status = "anonymous"` (used by 401 handler).
- Initial `status`: `"loading"` if a token exists in sessionStorage else `"anonymous"`.

Thunks (createAsyncThunk):
- `loginThunk({ slug, email, password })` → on `none`: persist session, dispatch `fetchMe`;
  on `verify`/`enroll`: set `mfa`.
- `verifyMfaThunk({ slug, mfaToken, code })` → persist session, clear `mfa`, `fetchMe`.
- `enrollActivateThunk({ slug, mfaToken, code })` → persist session, clear `mfa`, returns
  `recovery_codes`, `fetchMe`.
- `fetchMe()` → hydrate `user`, set `authenticated`; on failure clear + `anonymous`.
- `keepaliveThunk()` → extend idle window; 401 path handled by interceptor.
- `logoutThunk()` → best-effort logout call, then clear.

Screen-local thunks (not stored in auth state, dispatched & awaited by their screens):
- `acceptInviteThunk`, `activateInviteMfaThunk`, `inviteUserThunk`.

### API layer
- Rewrite `src/lib/auth/api.ts` to the verified contract (correct paths/fields, slug-less
  me/logout/keepalive, add enroll-activate, accept-invite, activate-invite-mfa, invite-user).
- `inviteUser` sends `Idempotency-Key: crypto.randomUUID()`.
- `src/lib/api/client.ts`: keep envelope/`ApiError`; add `registerAuthFailureHandler(fn)`.
  - **401** → call the handler (store dispatches `forceLogout`; `status` → anonymous →
    `RequireAuth` redirects to login). Distinct from 403.
  - **403** → throw `ApiError(403)`; callers / `RequirePermission` show access-denied. No logout.
- `storage.ts` kept as-is (sessionStorage token + slug).

### Routing — `App.tsx`
- `/tenants/:tenantSlug/login`
- `/tenants/:tenantSlug/mfa`            (verify)
- `/tenants/:tenantSlug/mfa-enroll`     (first-login enroll-activate)
- `/tenants/:tenantSlug/accept-invite`  (public; reads `?token=`)
- `/login` (no slug) → redirect to `/tenants/{VITE_DEFAULT_TENANT_SLUG}/login` (dev convenience).
- Protected app routes unchanged, behind `RequireAuth`.

### Components
- `RequireAuth` (rewritten): reads `status` from Redux. loading → spinner; anonymous →
  `<Navigate>` to tenant login with `state.from`.
- `RequirePermission` + `usePermission(code)` hook: conditional UI + route-level access-denied.
- `<IdleManager>`: see "Idle auto-logout" below. Mounted only while authenticated.

### Idle auto-logout (with pre-logout warning)
Aligned to the backend session model (idle TTL ~1h, absolute cap ~12h). Centralized as a single
`<IdleManager>` component rendered inside `AppShell` (which only mounts behind `RequireAuth`, i.e.
while authenticated), so all listeners/timers are torn down automatically on logout (unmount).

- **Activity tracking:** listens for `mousemove`, `mousedown`, `keydown`, `scroll`, `touchstart`
  (passive). Any activity updates a `lastActivity` timestamp (a ref — no re-render).
- **Throttled keepalive:** on activity, if `now - lastKeepalive ≥ KEEPALIVE_THROTTLE_MS` (default
  5 min), dispatch `keepaliveThunk` to slide the backend idle window; update `lastKeepalive`.
- **Real elapsed time:** a 1s interval recomputes deadlines from `Date.now()` (not tick counting),
  and a `visibilitychange` + `focus` listener forces an immediate recompute so a backgrounded /
  slept tab logs out correctly on return.
- **Deadlines:** `idleDeadline = lastActivity + IDLE_TIMEOUT_MS` (default 60 min);
  `absoluteDeadline = sessionStart + ABSOLUTE_MAX_MS` (12h, from a stored login timestamp);
  `logoutAt = min(idleDeadline, absoluteDeadline)`. The absolute cap therefore forces logout even
  under continuous activity. `keepaliveThunk` 401 (cap reached) is a backend-side fallback →
  `forceLogout`.
- **Warning:** when `logoutAt - now ≤ WARNING_LEAD_MS` (default 60s), show a (non-dismissable)
  modal with a live countdown ("Logging out in 60s…"). While the warning is shown, passive
  activity does NOT reset the timer — only the explicit action does (otherwise the warning could
  never appear / would vanish on mouse-move).
- **"Stay signed in":** resets `lastActivity = now`, dispatches `keepaliveThunk`, hides the modal.
- **Timeout:** if the countdown hits 0, dispatch `logoutThunk` (POST `/auth/logout` + clear state),
  then navigate to `/tenants/{slug}/login` (slug captured before clearing).
- **Configurable constants** in `src/lib/auth/idle.ts`: `IDLE_TIMEOUT_MS`, `WARNING_LEAD_MS`,
  `KEEPALIVE_THROTTLE_MS`, `ABSOLUTE_MAX_MS`.
- **Session start** is stamped into `sessionStorage` by `setSession` (and cleared by `clearSession`)
  so the absolute deadline survives a refresh.

### Screens (Tailwind + shadcn/ui; mirror existing `Login.tsx` patterns: useState + busy + ApiError)
1. **Login** — `loginThunk`; branch on `mfa`. 401 → single generic "Invalid credentials."
2. **MFA Verify** — `{ mfa_token, code }`; TOTP or recovery code (one field + helper text).
3. **MFA Enroll** — QR from `provisioning_uri` via **`qrcode.react`** (+ secret fallback);
   confirm code → show 10 recovery codes prominently (copy/download) with a
   "I've saved these" gate before entering the app.
4. **Accept Invite** — token from query + slug from path; set-password → `acceptInvite`.
   If `mfa_required` → QR + recovery-codes step (`activateInviteMfa`) → redirect to login
   ("account active, please sign in"). Handle invalid/expired/used tokens.
5. **Admin invite** — `InviteUserDialog` (shadcn `Dialog`) triggered by a button rendered only
   when `permissions.includes("users:manage")`; surfaced in Settings. Shows returned
   `invite_url` to copy.

## Error handling
- Login 401 → generic "Invalid credentials." (no enumeration).
- 403 → distinct access-denied (no logout).
- Network → "Could not reach the server."
- Invalid/expired/used invite → explicit message.

## Testing / verification
- Vitest unit tests for slice reducers + thunks (mocked api) across all branches.
- Manual E2E: login with/without MFA; MFA enroll; verify with TOTP and with a recovery code;
  admin sends invite; invited user accepts; keepalive; logout; 401/403; RBAC-gated invite
  button hidden for non-admins.
- Idle auto-logout: activity resets the timer + triggers throttled keepalive; warning appears
  exactly 1 min before logout with a working countdown; "Stay signed in" cancels + extends;
  inaction completes auto-logout and redirects to login; absolute 12h cap forces logout even
  under continuous activity. Unit-test the pure deadline/countdown helper with injected `now`.

## Out of scope
- Self-service password reset (Phase 1 — does not exist on backend).
- RTK Query, react-hook-form (project uses useState + Zod).

## New dependencies
- `@reduxjs/toolkit`, `react-redux`, `qrcode.react`.
