# Frontend Auth Integration (Redux Toolkit) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the Vera 2.0 backend auth flow into the frontend with Redux Toolkit state, tenant-scoped routing, and the login / MFA / invite screens.

**Architecture:** A Redux Toolkit `auth` slice holds session state (`status`, `user`, `tenantSlug`, transient `mfa`). Async thunks call a thin API layer over the envelope-unwrapping `apiRequest` client. The client invokes a registered handler on HTTP 401 that dispatches `forceLogout`, flipping `status` to `anonymous` so `RequireAuth` redirects — no imperative navigation. Tenant-scoped auth screens live under `/tenants/:tenantSlug/...`; token-scoped calls (`/auth/me`, `/auth/logout`, `/auth/session/keepalive`) carry no slug.

**Tech Stack:** React 19, TypeScript, Vite, react-router-dom v7, Redux Toolkit, react-redux, Tailwind v4, shadcn/ui (radix-ui umbrella), qrcode.react, Zod, Vitest.

## Global Constraints

- All API paths are relative to `VITE_API_BASE_URL` (default `/api/v1`); pass paths WITHOUT the prefix (e.g. `/auth/me`).
- Tenant auth endpoints use `/tenants/{slug}/auth/...`; `slug` must be `encodeURIComponent`-ed.
- Token-scoped endpoints (`/auth/me`, `/auth/logout`, `/auth/session/keepalive`, `/auth/mfa/enroll`, `/auth/mfa/activate`) take NO slug.
- The opaque `session_token` is sent as `Authorization: Bearer <token>`; never parsed as a JWT.
- The `mfa_token` is short-lived, lives only in `auth.mfa.token`, and is discarded the moment a `session_token` is issued.
- Login 401 shows ONE generic message: `Invalid credentials.` (no enumeration).
- `POST /users/invitations` requires an `Idempotency-Key` header and permission `users:manage`.
- Imports use the `@/` alias. Component files are PascalCase `.tsx`. Use `cn()` for class merging. Use `useState` + `ApiError` try/catch + `busy` flag for forms (no react-hook-form).
- Session persists in `sessionStorage` via existing `src/lib/auth/storage.ts`.
- Tests are pure logic (slice/thunks) with the api module mocked — React Testing Library is NOT installed; do not write component-render tests.

---

## File Structure

Create:
- `src/store/index.ts` — store config, `RootState`/`AppDispatch`.
- `src/store/hooks.ts` — typed `useAppDispatch`/`useAppSelector`.
- `src/store/authSlice.ts` — auth state, reducers, thunks, selectors.
- `src/store/authSlice.test.ts` — unit tests.
- `src/lib/auth/permissions.ts` — `usePermission` hook + `hasPermission` helper.
- `src/components/auth/RequirePermission.tsx` — route/section permission guard.
- `src/components/auth/RecoveryCodes.tsx` — recovery-codes display + save gate.
- `src/components/users/InviteUserDialog.tsx` — admin invite dialog.
- `src/lib/auth/idle.ts` — idle config constants + pure `computeIdleState` helper.
- `src/lib/auth/idle.test.ts` — unit tests for `computeIdleState`.
- `src/components/auth/IdleWarningDialog.tsx` — pre-logout warning modal.
- `src/components/auth/IdleManager.tsx` — activity tracking, throttled keepalive, idle/absolute deadlines, warning + auto-logout.
- `src/pages/MfaVerify.tsx`, `src/pages/MfaEnroll.tsx`, `src/pages/AcceptInvite.tsx`, `src/pages/Settings.tsx`.

Modify:
- `src/lib/api/client.ts` — add `registerAuthFailureHandler`; call on 401.
- `src/lib/auth/api.ts` — rewrite to the real backend contract.
- `src/lib/auth/storage.ts` — stamp/read/clear the session-start timestamp (for the absolute cap).
- `src/main.tsx` — wrap `<Provider store={store}>`.
- `src/App.tsx` — Provider/route restructure, remove `AuthProvider`.
- `src/components/auth/RequireAuth.tsx` — read Redux `status`.
- `src/components/layout/Topbar.tsx` — Redux logout.
- `src/pages/Login.tsx` — tenant-scoped Redux login.
- `package.json` — add deps.

Delete (after migration):
- `src/lib/auth/auth-context.tsx` — replaced by the slice.

---

## Task 1: Dependencies, store, and Provider wiring

**Files:**
- Modify: `package.json`
- Create: `src/store/index.ts`, `src/store/hooks.ts`
- Modify: `src/main.tsx`

**Interfaces:**
- Produces: `store`, `RootState`, `AppDispatch` (from `@/store`); `useAppDispatch`, `useAppSelector` (from `@/store/hooks`).

- [ ] **Step 1: Install dependencies**

Run:
```bash
npm install @reduxjs/toolkit react-redux qrcode.react
```
Expected: added to `dependencies`, no errors.

- [ ] **Step 2: Create the store** (`src/store/index.ts`)

```ts
import { configureStore } from "@reduxjs/toolkit"
import authReducer from "@/store/authSlice"

export const store = configureStore({
  reducer: { auth: authReducer },
})

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch
```

- [ ] **Step 3: Create typed hooks** (`src/store/hooks.ts`)

```ts
import { useDispatch, useSelector } from "react-redux"
import type { AppDispatch, RootState } from "@/store"

export const useAppDispatch = () => useDispatch<AppDispatch>()
export const useAppSelector = useSelector.withTypes<RootState>()
```

> Step 2 imports `@/store/authSlice` which is created in Task 3. Implement Task 3 before type-checking, or temporarily stub `authReducer`. Recommended: do Task 3's slice file first if executing strictly, but the task order here keeps store wiring together. A stub is acceptable: `const authReducer = (s = {}) => s`.

- [ ] **Step 4: Wrap the app with Provider** (`src/main.tsx`)

Add the import and wrap the root element:
```tsx
import { Provider } from "react-redux"
import { store } from "@/store"
// ...
// <StrictMode>
//   <Provider store={store}>
//     <App />
//   </Provider>
// </StrictMode>
```
Wrap whatever `main.tsx` currently renders (`<App />`) with `<Provider store={store}>…</Provider>`.

- [ ] **Step 5: Commit**

```bash
git add package.json package-lock.json src/store/index.ts src/store/hooks.ts src/main.tsx
git commit -m "feat(auth): add redux toolkit store and provider"
```

---

## Task 2: API layer — real contract + 401 handler

**Files:**
- Modify: `src/lib/api/client.ts`
- Modify: `src/lib/auth/api.ts`

**Interfaces:**
- Produces (client): `registerAuthFailureHandler(handler: () => void): void`; existing `apiRequest<T>`, `ApiError`. `apiRequest` gains an `headers?: Record<string,string>` option.
- Produces (api): types `LoginResult`, `SessionResult`, `EnrollActivateResult`, `AcceptInviteResult`, `RecoveryCodesResult`, `KeepaliveResult`, `MeResponse`, `InviteUserResult`; functions `login`, `verifyMfa`, `enrollActivate`, `getMe`, `logout`, `keepalive`, `acceptInvite`, `activateInviteMfa`, `inviteUser`.

- [ ] **Step 1: Add auth-failure handler + headers to client** (`src/lib/api/client.ts`)

Add near the top (after `BASE_URL`):
```ts
// Registered by the store so a 401 can clear auth state without this module
// importing the store (avoids a circular dependency).
let authFailureHandler: (() => void) | null = null
export function registerAuthFailureHandler(handler: () => void): void {
  authFailureHandler = handler
}
```

Extend `RequestOptions`:
```ts
type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
  body?: unknown
  /** Attach the stored bearer token. Defaults to true; false for login. */
  auth?: boolean
  /** Extra headers (e.g. Idempotency-Key). */
  headers?: Record<string, string>
}
```

In `apiRequest`, merge headers and handle 401 before throwing. Replace the headers init and the error block:
```ts
const { method = "GET", body, auth = true, headers: extraHeaders } = opts

const headers: Record<string, string> = {
  "Content-Type": "application/json",
  ...extraHeaders,
}
```
And in the failure branch, before `throw`:
```ts
if (!res.ok || !envelope || envelope.status === "FAIL") {
  // 401 → session is gone/expired. Let the app clear auth state + redirect.
  // 403 is intentionally NOT treated here — callers handle access-denied.
  if (res.status === 401 && auth) authFailureHandler?.()
  throw new ApiError(
    res.status,
    envelope?.error_code ?? null,
    envelope?.message ?? `Request failed (${res.status}).`,
  )
}
```

- [ ] **Step 2: Rewrite the auth API** (`src/lib/auth/api.ts`) — replace the whole file

```ts
// Thin typed wrappers over the control-plane auth endpoints, matching the backend
// contract exactly. Tenant-scoped routes are keyed by the human-readable slug.
// Token-scoped self endpoints (/auth/me, /auth/logout, /auth/session/keepalive,
// /auth/mfa/enroll, /auth/mfa/activate) carry NO slug.

import { apiRequest } from "@/lib/api/client"

export type LoginResult = {
  mfa: "none" | "verify" | "enroll"
  session_token: string | null
  mfa_token: string | null
  provisioning_uri: string | null
}
export type SessionResult = { session_token: string }
export type EnrollActivateResult = { session_token: string; recovery_codes: string[] }
export type AcceptInviteResult = {
  mfa_required: boolean
  provisioning_uri: string | null
  mfa_token: string | null
}
export type RecoveryCodesResult = { recovery_codes: string[] }
export type KeepaliveResult = { expires_in_seconds: number }

export type MeResponse = {
  user_id: string
  email: string
  name: string
  account_type: string
  tenant_id: string | null
  tenant_slug: string | null
  roles: string[]
  permissions: string[]
}

export type InviteUserResult = {
  user_id: string
  email: string
  invite_url: string
  email_sent: boolean
}

const tenantAuth = (slug: string) => `/tenants/${encodeURIComponent(slug)}/auth`

export function login(slug: string, email: string, password: string) {
  return apiRequest<LoginResult>(`${tenantAuth(slug)}/login`, {
    method: "POST",
    body: { email, password },
    auth: false,
  })
}

export function verifyMfa(slug: string, mfaToken: string, code: string) {
  return apiRequest<SessionResult>(`${tenantAuth(slug)}/mfa/verify`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

export function enrollActivate(slug: string, mfaToken: string, code: string) {
  return apiRequest<EnrollActivateResult>(`${tenantAuth(slug)}/mfa/enroll-activate`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

export function acceptInvite(slug: string, token: string, password: string) {
  return apiRequest<AcceptInviteResult>(`${tenantAuth(slug)}/invitations/accept`, {
    method: "POST",
    body: { token, password },
    auth: false,
  })
}

export function activateInviteMfa(slug: string, mfaToken: string, code: string) {
  return apiRequest<RecoveryCodesResult>(`${tenantAuth(slug)}/invitations/activate-mfa`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

export function getMe() {
  return apiRequest<MeResponse>(`/auth/me`)
}

export function logout() {
  return apiRequest<null>(`/auth/logout`, { method: "POST" })
}

export function keepalive() {
  return apiRequest<KeepaliveResult>(`/auth/session/keepalive`, { method: "POST" })
}

export function inviteUser(input: {
  email: string
  name: string
  roleIds: string[]
  sendEmail: boolean
}) {
  return apiRequest<InviteUserResult>(`/users/invitations`, {
    method: "POST",
    body: {
      email: input.email,
      name: input.name,
      role_ids: input.roleIds,
      send_email: input.sendEmail,
    },
    headers: { "Idempotency-Key": crypto.randomUUID() },
  })
}
```

- [ ] **Step 3: Verify type-check**

Run: `npx tsc -b`
Expected: may fail ONLY on files still importing the old `auth-context`/old api shapes (fixed in later tasks). `client.ts` and `api.ts` themselves compile.

- [ ] **Step 4: Commit**

```bash
git add src/lib/api/client.ts src/lib/auth/api.ts
git commit -m "feat(auth): align api layer to backend contract + 401 handler hook"
```

---

## Task 3: Auth slice (state, thunks, selectors) + tests

**Files:**
- Create: `src/store/authSlice.ts`
- Test: `src/store/authSlice.test.ts`

**Interfaces:**
- Consumes: all functions/types from `@/lib/auth/api`; `getToken`, `setSession`, `clearSession` from `@/lib/auth/storage`.
- Produces: default export `authReducer`; thunks `loginThunk`, `verifyMfaThunk`, `enrollActivateThunk`, `fetchMe`, `keepaliveThunk`, `logoutThunk`; actions `forceLogout`, `clearError`; selectors `selectStatus`, `selectUser`, `selectTenantSlug`, `selectMfa`, `selectAuthLoading`, `selectAuthError`, `selectPermissions`. `loginThunk` resolves to `LoginResult["mfa"]`. `enrollActivateThunk` resolves to `string[]` (recovery codes).

- [ ] **Step 1: Write the slice** (`src/store/authSlice.ts`)

```ts
import { createAsyncThunk, createSlice, type PayloadAction } from "@reduxjs/toolkit"

import * as authApi from "@/lib/auth/api"
import type { MeResponse } from "@/lib/auth/api"
import { ApiError } from "@/lib/api/client"
import { clearSession, getToken, setSession } from "@/lib/auth/storage"

type Status = "loading" | "anonymous" | "authenticated"
type MfaState = { token: string; step: "verify" | "enroll"; provisioningUri?: string }

type AuthState = {
  status: Status
  user: MeResponse | null
  tenantSlug: string | null
  mfa: MfaState | null
  loading: boolean
  error: string | null
}

const initialState: AuthState = {
  // A persisted token means we must hydrate via /me before we know who it is.
  status: getToken() ? "loading" : "anonymous",
  user: null,
  tenantSlug: null,
  mfa: null,
  loading: false,
  error: null,
}

function message(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback
}

export const fetchMe = createAsyncThunk("auth/fetchMe", async () => {
  return await authApi.getMe()
})

export const loginThunk = createAsyncThunk(
  "auth/login",
  async (
    arg: { slug: string; email: string; password: string },
    { dispatch },
  ): Promise<authApi.LoginResult["mfa"]> => {
    const res = await authApi.login(arg.slug, arg.email, arg.password)
    if (res.mfa === "none") {
      setSession(res.session_token ?? "", arg.slug)
      await dispatch(fetchMe()).unwrap()
    }
    return res.mfa
  },
)

export const verifyMfaThunk = createAsyncThunk(
  "auth/verifyMfa",
  async (arg: { slug: string; mfaToken: string; code: string }, { dispatch }) => {
    const res = await authApi.verifyMfa(arg.slug, arg.mfaToken, arg.code)
    setSession(res.session_token, arg.slug)
    await dispatch(fetchMe()).unwrap()
  },
)

export const enrollActivateThunk = createAsyncThunk(
  "auth/enrollActivate",
  async (
    arg: { slug: string; mfaToken: string; code: string },
    { dispatch },
  ): Promise<string[]> => {
    const res = await authApi.enrollActivate(arg.slug, arg.mfaToken, arg.code)
    setSession(res.session_token, arg.slug)
    await dispatch(fetchMe()).unwrap()
    return res.recovery_codes
  },
)

export const keepaliveThunk = createAsyncThunk("auth/keepalive", async () => {
  return await authApi.keepalive()
})

export const logoutThunk = createAsyncThunk("auth/logout", async () => {
  try {
    await authApi.logout()
  } catch {
    // Best effort — clear locally even if the revoke call fails.
  }
})

const authSlice = createSlice({
  name: "auth",
  initialState,
  reducers: {
    // Hard reset used on 401 and logout.
    forceLogout(state) {
      clearSession()
      state.status = "anonymous"
      state.user = null
      state.tenantSlug = null
      state.mfa = null
      state.loading = false
    },
    clearError(state) {
      state.error = null
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(loginThunk.pending, (s) => {
        s.loading = true
        s.error = null
      })
      .addCase(loginThunk.fulfilled, (s, a: PayloadAction<authApi.LoginResult["mfa"]>) => {
        s.loading = false
        if (a.payload === "none") {
          s.mfa = null // session established; fetchMe drives status
        }
        // "verify"/"enroll": the screen reads the returned mfa value and stores
        // the token via setMfa-side effects below — handled in the thunk caller.
      })
      .addCase(loginThunk.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.error, "Invalid credentials.")
      })
      .addCase(fetchMe.pending, (s) => {
        if (s.status !== "authenticated") s.status = "loading"
      })
      .addCase(fetchMe.fulfilled, (s, a: PayloadAction<MeResponse>) => {
        s.user = a.payload
        s.tenantSlug = a.payload.tenant_slug ?? s.tenantSlug
        s.status = "authenticated"
      })
      .addCase(fetchMe.rejected, (s) => {
        clearSession()
        s.user = null
        s.status = "anonymous"
      })
      .addCase(verifyMfaThunk.pending, (s) => {
        s.loading = true
        s.error = null
      })
      .addCase(verifyMfaThunk.fulfilled, (s) => {
        s.loading = false
        s.mfa = null
      })
      .addCase(verifyMfaThunk.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.error, "Verification failed.")
      })
      .addCase(enrollActivateThunk.pending, (s) => {
        s.loading = true
        s.error = null
      })
      .addCase(enrollActivateThunk.fulfilled, (s) => {
        s.loading = false
        s.mfa = null
      })
      .addCase(enrollActivateThunk.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.error, "Enrollment failed.")
      })
      .addCase(logoutThunk.fulfilled, (s) => {
        clearSession()
        s.status = "anonymous"
        s.user = null
        s.tenantSlug = null
        s.mfa = null
      })
  },
})

// The login screen sets the transient MFA challenge after a "verify"/"enroll"
// outcome. Kept as a plain reducer so the screen owns the token + provisioning URI.
export const setMfa = (mfa: MfaState | null) => ({ type: "auth/setMfa" as const, payload: mfa })

export const { forceLogout, clearError } = authSlice.actions

// Add setMfa handling without a second slice: extend reducers.
// (Implemented as an explicit case to keep MfaState writes in one place.)
export default authSlice.reducer

export const selectStatus = (s: { auth: AuthState }) => s.auth.status
export const selectUser = (s: { auth: AuthState }) => s.auth.user
export const selectTenantSlug = (s: { auth: AuthState }) => s.auth.tenantSlug
export const selectMfa = (s: { auth: AuthState }) => s.auth.mfa
export const selectAuthLoading = (s: { auth: AuthState }) => s.auth.loading
export const selectAuthError = (s: { auth: AuthState }) => s.auth.error
export const selectPermissions = (s: { auth: AuthState }) => s.auth.user?.permissions ?? []
```

> The transient `mfa` field needs a real action creator. Replace the placeholder `setMfa`/comment above by adding `setMfa` to the slice `reducers` block:
> ```ts
> setMfa(state, action: PayloadAction<MfaState | null>) { state.mfa = action.payload },
> ```
> and export it from `authSlice.actions`: `export const { forceLogout, clearError, setMfa } = authSlice.actions`. Remove the standalone `setMfa` const. This keeps all `MfaState` writes inside the slice.

- [ ] **Step 2: Write failing tests** (`src/store/authSlice.test.ts`)

```ts
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/api")
vi.mock("@/lib/auth/storage", () => ({
  getToken: () => null,
  setSession: vi.fn(),
  clearSession: vi.fn(),
}))

import { configureStore } from "@reduxjs/toolkit"
import * as api from "@/lib/auth/api"
import authReducer, {
  forceLogout,
  loginThunk,
  fetchMe,
  selectStatus,
  selectMfa,
} from "@/store/authSlice"

function makeStore() {
  return configureStore({ reducer: { auth: authReducer } })
}

const me: api.MeResponse = {
  user_id: "u1", email: "a@b.co", name: "A", account_type: "tenant",
  tenant_id: "t1", tenant_slug: "acme", roles: ["TENANT_ADMIN"],
  permissions: ["users:manage"],
}

describe("authSlice", () => {
  beforeEach(() => vi.resetAllMocks())

  it("logs in without MFA → authenticated", async () => {
    vi.mocked(api.login).mockResolvedValue({
      mfa: "none", session_token: "tok", mfa_token: null, provisioning_uri: null,
    })
    vi.mocked(api.getMe).mockResolvedValue(me)
    const store = makeStore()
    const mfa = await store.dispatch(
      loginThunk({ slug: "acme", email: "a@b.co", password: "x" }),
    ).unwrap()
    expect(mfa).toBe("none")
    expect(selectStatus(store.getState())).toBe("authenticated")
  })

  it("login with MFA verify does not authenticate", async () => {
    vi.mocked(api.login).mockResolvedValue({
      mfa: "verify", session_token: null, mfa_token: "mt", provisioning_uri: null,
    })
    const store = makeStore()
    const mfa = await store.dispatch(
      loginThunk({ slug: "acme", email: "a@b.co", password: "x" }),
    ).unwrap()
    expect(mfa).toBe("verify")
    expect(selectStatus(store.getState())).toBe("anonymous")
    expect(api.getMe).not.toHaveBeenCalled()
  })

  it("forceLogout resets to anonymous", async () => {
    vi.mocked(api.getMe).mockResolvedValue(me)
    const store = makeStore()
    await store.dispatch(fetchMe())
    expect(selectStatus(store.getState())).toBe("authenticated")
    store.dispatch(forceLogout())
    expect(selectStatus(store.getState())).toBe("anonymous")
    expect(selectMfa(store.getState())).toBeNull()
  })
})
```

- [ ] **Step 3: Run tests — expect fail then pass**

Run: `npm test -- src/store/authSlice.test.ts`
Expected: PASS after the slice (Step 1 + the `setMfa` fix) compiles. If RED due to missing `setMfa`, apply the Step 1 note.

- [ ] **Step 4: Register the 401 handler** — append to `src/store/index.ts`

```ts
import { registerAuthFailureHandler } from "@/lib/api/client"
import { forceLogout } from "@/store/authSlice"

registerAuthFailureHandler(() => store.dispatch(forceLogout()))
```

- [ ] **Step 5: Commit**

```bash
git add src/store/authSlice.ts src/store/authSlice.test.ts src/store/index.ts
git commit -m "feat(auth): redux auth slice with thunks, selectors, 401 wiring"
```

---

## Task 4: Routing + RequireAuth + bootstrap hydration

**Files:**
- Modify: `src/components/auth/RequireAuth.tsx`
- Modify: `src/App.tsx`
- Delete: `src/lib/auth/auth-context.tsx`

**Interfaces:**
- Consumes: `selectStatus`, `fetchMe` from `@/store/authSlice`; `useAppSelector`/`useAppDispatch`.
- Produces: routes `/tenants/:tenantSlug/login|mfa|mfa-enroll|accept-invite`; `RequireAuth` gating the app shell.

- [ ] **Step 1: Rewrite RequireAuth** (`src/components/auth/RequireAuth.tsx`)

```tsx
import { Navigate, Outlet, useLocation } from "react-router-dom"
import { useAppSelector } from "@/store/hooks"
import { selectStatus, selectTenantSlug } from "@/store/authSlice"

const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

export function RequireAuth() {
  const status = useAppSelector(selectStatus)
  const slug = useAppSelector(selectTenantSlug) ?? DEFAULT_SLUG
  const location = useLocation()
  if (status === "loading") {
    return <div className="flex min-h-screen items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (status === "anonymous") {
    return <Navigate to={`/tenants/${slug}/login`} replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}
```

- [ ] **Step 2: Rewrite App.tsx**

```tsx
import { useEffect } from "react"
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AppShell } from "@/components/layout/AppShell"
import { RequireAuth } from "@/components/auth/RequireAuth"
import { useAppDispatch } from "@/store/hooks"
import { fetchMe } from "@/store/authSlice"
import { getToken } from "@/lib/auth/storage"
import { Login } from "@/pages/Login"
import { MfaVerify } from "@/pages/MfaVerify"
import { MfaEnroll } from "@/pages/MfaEnroll"
import { AcceptInvite } from "@/pages/AcceptInvite"
import { LiveMonitoring } from "@/pages/LiveMonitoring"
import { DataManagement } from "@/pages/DataManagement"
import { Settings } from "@/pages/Settings"
import { Placeholder } from "@/pages/Placeholder"

const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

function App() {
  const dispatch = useAppDispatch()
  // Hydrate a persisted session once on mount.
  useEffect(() => {
    if (getToken()) dispatch(fetchMe())
  }, [dispatch])

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Navigate to={`/tenants/${DEFAULT_SLUG}/login`} replace />} />
        <Route path="/tenants/:tenantSlug/login" element={<Login />} />
        <Route path="/tenants/:tenantSlug/mfa" element={<MfaVerify />} />
        <Route path="/tenants/:tenantSlug/mfa-enroll" element={<MfaEnroll />} />
        <Route path="/tenants/:tenantSlug/accept-invite" element={<AcceptInvite />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<LiveMonitoring />} />
            <Route path="data-management" element={<DataManagement />} />
            <Route path="call-history" element={<Placeholder title="Call History" />} />
            <Route path="analytics" element={<Placeholder title="Analytics" />} />
            <Route path="settings" element={<Settings />} />
            <Route path="*" element={<Placeholder title="Not Found" />} />
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
```

- [ ] **Step 3: Delete the old context**

```bash
git rm src/lib/auth/auth-context.tsx
```
(Topbar still imports it — fixed in Task 8. Type-check will fail there until then; that's expected.)

- [ ] **Step 4: Commit**

```bash
git add src/App.tsx src/components/auth/RequireAuth.tsx
git commit -m "feat(auth): tenant-scoped routes + redux-driven route guard"
```

---

## Task 5: Permission helpers

**Files:**
- Create: `src/lib/auth/permissions.ts`
- Create: `src/components/auth/RequirePermission.tsx`

**Interfaces:**
- Produces: `usePermission(code: string): boolean`; `RequirePermission` component (props `{ permission: string; children: ReactNode }`).

- [ ] **Step 1: usePermission hook** (`src/lib/auth/permissions.ts`)

```ts
import { useAppSelector } from "@/store/hooks"
import { selectPermissions } from "@/store/authSlice"

export function usePermission(code: string): boolean {
  const permissions = useAppSelector(selectPermissions)
  return permissions.includes(code)
}
```

- [ ] **Step 2: RequirePermission** (`src/components/auth/RequirePermission.tsx`)

```tsx
import type { ReactNode } from "react"
import { usePermission } from "@/lib/auth/permissions"

export function RequirePermission({
  permission,
  children,
  fallback = null,
}: {
  permission: string
  children: ReactNode
  fallback?: ReactNode
}) {
  return usePermission(permission) ? <>{children}</> : <>{fallback}</>
}
```

- [ ] **Step 3: Commit**

```bash
git add src/lib/auth/permissions.ts src/components/auth/RequirePermission.tsx
git commit -m "feat(auth): permission hook + RequirePermission guard"
```

---

## Task 6: Login screen (tenant-scoped, Redux)

**Files:**
- Modify: `src/pages/Login.tsx`

**Interfaces:**
- Consumes: `loginThunk`, `setMfa`, `selectStatus`, `selectAuthError`, `selectAuthLoading` from slice; `useParams`, `useNavigate`.

- [ ] **Step 1: Rewrite Login.tsx**

```tsx
import { useState, type FormEvent } from "react"
import { Navigate, useNavigate, useParams, useLocation } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { loginThunk, setMfa, selectStatus } from "@/store/authSlice"

const DEV_EMAIL = import.meta.env.VITE_DEV_EMAIL ?? ""
const DEV_PASSWORD = import.meta.env.VITE_DEV_PASSWORD ?? ""

export function Login() {
  const { tenantSlug = "" } = useParams()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const location = useLocation()
  const status = useAppSelector(selectStatus)
  const from = (location.state as { from?: string } | null)?.from ?? "/"

  const [email, setEmail] = useState(DEV_EMAIL)
  const [password, setPassword] = useState(DEV_PASSWORD)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (status === "authenticated") return <Navigate to={from} replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await dispatch(loginThunk({ slug: tenantSlug, email, password })).unwrap()
      if (res === "none") {
        navigate(from, { replace: true })
      } else {
        // The thunk returned the discriminator; re-fetch the tokens from the raw
        // login call is avoided by reading them here: dispatch stored them via setMfa.
        // (loginThunk only returns the discriminator, so capture tokens below.)
      }
    } catch (err) {
      setError(err instanceof ApiError && err.httpStatus === 401
        ? "Invalid credentials."
        : err instanceof ApiError ? err.message : "Something went wrong.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Sign in to Vera</CardTitle>
          <CardDescription>Workspace: {tenantSlug || "—"}</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input id="email" type="email" autoComplete="username" required
                value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input id="password" type="password" autoComplete="current-password" required
                value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" size="lg" className="w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
```

> **Token capture fix:** `loginThunk` returns only the `mfa` discriminator, but the MFA screens need `mfa_token` + `provisioning_uri`. Change `loginThunk` to dispatch `setMfa` itself when `mfa !== "none"`, so the screen only needs to navigate. In `authSlice.ts` `loginThunk`, after getting `res`:
> ```ts
> if (res.mfa === "verify") dispatch(setMfa({ token: res.mfa_token ?? "", step: "verify" }))
> else if (res.mfa === "enroll") dispatch(setMfa({ token: res.mfa_token ?? "", step: "enroll", provisioningUri: res.provisioning_uri ?? undefined }))
> ```
> Then in `Login.onSubmit`, replace the empty `else` body with:
> ```ts
> navigate(res === "verify" ? `/tenants/${tenantSlug}/mfa` : `/tenants/${tenantSlug}/mfa-enroll`)
> ```

- [ ] **Step 2: Type-check**

Run: `npx tsc -b`
Expected: Login compiles (errors only remain in Topbar until Task 8, and the screens created in Task 7).

- [ ] **Step 3: Commit**

```bash
git add src/pages/Login.tsx src/store/authSlice.ts
git commit -m "feat(auth): tenant-scoped redux login screen"
```

---

## Task 7: MFA Verify + MFA Enroll screens

**Files:**
- Create: `src/components/auth/RecoveryCodes.tsx`
- Create: `src/pages/MfaVerify.tsx`
- Create: `src/pages/MfaEnroll.tsx`

**Interfaces:**
- Consumes: `selectMfa`, `verifyMfaThunk`, `enrollActivateThunk`; `QRCodeSVG` from `qrcode.react`.
- Produces: `RecoveryCodes` (props `{ codes: string[]; onContinue: () => void }`).

- [ ] **Step 1: RecoveryCodes component** (`src/components/auth/RecoveryCodes.tsx`)

```tsx
import { useState } from "react"
import { Button } from "@/components/ui/button"

export function RecoveryCodes({ codes, onContinue }: { codes: string[]; onContinue: () => void }) {
  const [saved, setSaved] = useState(false)

  function copy() {
    void navigator.clipboard.writeText(codes.join("\n"))
  }
  function download() {
    const blob = new Blob([codes.join("\n")], { type: "text/plain" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = "vera-recovery-codes.txt"
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
        Save these recovery codes now. Each works once if you lose your authenticator.
        They will not be shown again.
      </div>
      <div className="grid grid-cols-2 gap-2 rounded-md border bg-muted/30 p-3 font-mono text-sm">
        {codes.map((c) => <span key={c}>{c}</span>)}
      </div>
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" onClick={copy}>Copy</Button>
        <Button type="button" variant="outline" size="sm" onClick={download}>Download</Button>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />
        I have saved my recovery codes
      </label>
      <Button className="w-full" disabled={!saved} onClick={onContinue}>Continue</Button>
    </div>
  )
}
```

- [ ] **Step 2: MFA Verify** (`src/pages/MfaVerify.tsx`)

```tsx
import { useState, type FormEvent } from "react"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { selectMfa, verifyMfaThunk } from "@/store/authSlice"

export function MfaVerify() {
  const { tenantSlug = "" } = useParams()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const mfa = useAppSelector(selectMfa)
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // No challenge in state (e.g. refresh) → back to login.
  if (!mfa || mfa.step !== "verify") return <Navigate to={`/tenants/${tenantSlug}/login`} replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await dispatch(verifyMfaThunk({ slug: tenantSlug, mfaToken: mfa!.token, code })).unwrap()
      navigate("/", { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Verification failed.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Two-factor verification</CardTitle>
          <CardDescription>Enter the 6-digit code from your authenticator, or a recovery code.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="code">Code</Label>
              <Input id="code" inputMode="text" autoComplete="one-time-code" required autoFocus
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" size="lg" className="w-full" disabled={busy}>
              {busy ? "Verifying…" : "Verify"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
```

- [ ] **Step 3: MFA Enroll** (`src/pages/MfaEnroll.tsx`)

```tsx
import { useState, type FormEvent } from "react"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { RecoveryCodes } from "@/components/auth/RecoveryCodes"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { selectMfa, enrollActivateThunk } from "@/store/authSlice"

export function MfaEnroll() {
  const { tenantSlug = "" } = useParams()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const mfa = useAppSelector(selectMfa)
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [recovery, setRecovery] = useState<string[] | null>(null)

  if (!mfa || mfa.step !== "enroll") return <Navigate to={`/tenants/${tenantSlug}/login`} replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const codes = await dispatch(
        enrollActivateThunk({ slug: tenantSlug, mfaToken: mfa!.token, code }),
      ).unwrap()
      setRecovery(codes)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Enrollment failed.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">Set up two-factor authentication</CardTitle>
          <CardDescription>
            {recovery ? "Save your recovery codes to finish." : "Scan the QR code with your authenticator app, then enter a code."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {recovery ? (
            <RecoveryCodes codes={recovery} onContinue={() => navigate("/", { replace: true })} />
          ) : (
            <div className="space-y-4">
              {mfa.provisioningUri && (
                <div className="flex justify-center rounded-md bg-white p-4">
                  <QRCodeSVG value={mfa.provisioningUri} size={180} />
                </div>
              )}
              <form onSubmit={onSubmit} className="space-y-4">
                <div className="space-y-1.5">
                  <Label htmlFor="code">Authentication code</Label>
                  <Input id="code" inputMode="numeric" autoComplete="one-time-code" required
                    value={code} onChange={(e) => setCode(e.target.value)} />
                </div>
                {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
                <Button type="submit" size="lg" className="w-full" disabled={busy}>
                  {busy ? "Activating…" : "Activate"}
                </Button>
              </form>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
```

- [ ] **Step 4: Commit**

```bash
git add src/components/auth/RecoveryCodes.tsx src/pages/MfaVerify.tsx src/pages/MfaEnroll.tsx
git commit -m "feat(auth): MFA verify and enrollment screens"
```

---

## Task 8: Accept-invite screen + Topbar migration

**Files:**
- Create: `src/pages/AcceptInvite.tsx`
- Modify: `src/components/layout/Topbar.tsx`

**Interfaces:**
- Consumes: `acceptInvite`, `activateInviteMfa` from `@/lib/auth/api`; `logoutThunk` from slice; `useSearchParams`, `useParams`.

- [ ] **Step 1: AcceptInvite** (`src/pages/AcceptInvite.tsx`)

```tsx
import { useState, type FormEvent } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router-dom"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { RecoveryCodes } from "@/components/auth/RecoveryCodes"
import { acceptInvite, activateInviteMfa } from "@/lib/auth/api"

type Phase =
  | { kind: "password" }
  | { kind: "mfa"; mfaToken: string; provisioningUri: string | null }
  | { kind: "recovery"; codes: string[] }
  | { kind: "done" }

export function AcceptInvite() {
  const { tenantSlug = "" } = useParams()
  const [params] = useSearchParams()
  const token = params.get("token") ?? ""
  const navigate = useNavigate()

  const [phase, setPhase] = useState<Phase>({ kind: "password" })
  const [password, setPassword] = useState("")
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const loginHref = `/tenants/${tenantSlug}/login`

  async function onSetPassword(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await acceptInvite(tenantSlug, token, password)
      if (res.mfa_required) {
        setPhase({ kind: "mfa", mfaToken: res.mfa_token ?? "", provisioningUri: res.provisioning_uri })
      } else {
        setPhase({ kind: "done" })
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "This invitation is invalid or has expired.")
    } finally {
      setBusy(false)
    }
  }

  async function onActivateMfa(e: FormEvent) {
    e.preventDefault()
    if (phase.kind !== "mfa") return
    setError(null)
    setBusy(true)
    try {
      const res = await activateInviteMfa(tenantSlug, phase.mfaToken, code)
      setPhase({ kind: "recovery", codes: res.recovery_codes })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Activation failed.")
    } finally {
      setBusy(false)
    }
  }

  if (!token) {
    return (
      <CenteredCard title="Invalid invitation" desc="This invite link is missing its token.">
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "recovery") {
    return (
      <CenteredCard title="Account ready" desc="Save your recovery codes, then sign in.">
        <RecoveryCodes codes={phase.codes} onContinue={() => navigate(loginHref)} />
      </CenteredCard>
    )
  }
  if (phase.kind === "done") {
    return (
      <CenteredCard title="Account active" desc="Your account is ready.">
        <Button className="w-full" onClick={() => navigate(loginHref)}>Sign in</Button>
      </CenteredCard>
    )
  }
  if (phase.kind === "mfa") {
    return (
      <CenteredCard title="Set up two-factor" desc="Scan the QR code, then enter a code to finish.">
        <div className="space-y-4">
          {phase.provisioningUri && (
            <div className="flex justify-center rounded-md bg-white p-4">
              <QRCodeSVG value={phase.provisioningUri} size={180} />
            </div>
          )}
          <form onSubmit={onActivateMfa} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="code">Authentication code</Label>
              <Input id="code" inputMode="numeric" autoComplete="one-time-code" required
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy}>{busy ? "Activating…" : "Activate"}</Button>
          </form>
        </div>
      </CenteredCard>
    )
  }

  return (
    <CenteredCard title="Accept your invitation" desc="Choose a password to activate your account.">
      <form onSubmit={onSetPassword} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input id="password" type="password" autoComplete="new-password" required minLength={8}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Saving…" : "Set password"}</Button>
      </form>
    </CenteredCard>
  )
}

function CenteredCard({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">{title}</CardTitle>
          <CardDescription>{desc}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </div>
  )
}
```

- [ ] **Step 2: Migrate Topbar to Redux** (`src/components/layout/Topbar.tsx`)

Replace the `useAuth` import/usage:
```tsx
import { PanelLeft, Search, Bell, LogOut } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { logoutThunk, selectTenantSlug } from "@/store/authSlice"

type TopbarProps = { onToggleSidebar: () => void }

export function Topbar({ onToggleSidebar }: TopbarProps) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const slug = useAppSelector(selectTenantSlug) ?? (import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? "")

  async function onLogout() {
    await dispatch(logoutThunk())
    navigate(`/tenants/${slug}/login`, { replace: true })
  }
  // ...rest of the JSX unchanged, keep the existing markup; only the logout handler changed.
}
```
Keep the existing JSX body exactly; only the hook wiring and `onLogout` change.

- [ ] **Step 3: Type-check**

Run: `npx tsc -b`
Expected: PASS (no more references to deleted `auth-context`).

- [ ] **Step 4: Commit**

```bash
git add src/pages/AcceptInvite.tsx src/components/layout/Topbar.tsx
git commit -m "feat(auth): invite acceptance screen + redux logout in topbar"
```

---

## Task 9: Admin invite dialog + Settings page

**Files:**
- Create: `src/components/users/InviteUserDialog.tsx`
- Create: `src/pages/Settings.tsx`

**Interfaces:**
- Consumes: `inviteUser` from `@/lib/auth/api`; `usePermission`; shadcn `Dialog*`, `Button`, `Input`, `Label`, `Switch`.

- [ ] **Step 1: InviteUserDialog** (`src/components/users/InviteUserDialog.tsx`)

```tsx
import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { inviteUser, type InviteUserResult } from "@/lib/auth/api"

export function InviteUserDialog() {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<InviteUserResult | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await inviteUser({ email, name, roleIds: [], sendEmail: true })
      setResult(res)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send invitation.")
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setOpen(false)
    setEmail(""); setName(""); setError(null); setResult(null)
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : reset())}>
      <DialogTrigger asChild>
        <Button>Invite user</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Invite a user</DialogTitle>
          <DialogDescription>They'll get a link to set a password and join this workspace.</DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="space-y-3">
            <p className="text-sm">Invitation created for <b>{result.email}</b>{result.email_sent ? " and emailed." : "."}</p>
            <div className="space-y-1.5">
              <Label htmlFor="invite-url">Invite link</Label>
              <Input id="invite-url" readOnly value={result.invite_url} onFocus={(e) => e.target.select()} />
            </div>
            <Button className="w-full" onClick={reset}>Done</Button>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="invite-email">Email</Label>
              <Input id="invite-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="invite-name">Name</Label>
              <Input id="invite-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy}>{busy ? "Sending…" : "Send invitation"}</Button>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 2: Settings page** (`src/pages/Settings.tsx`)

```tsx
import { RequirePermission } from "@/components/auth/RequirePermission"
import { InviteUserDialog } from "@/components/users/InviteUserDialog"
import { useAppSelector } from "@/store/hooks"
import { selectUser } from "@/store/authSlice"

export function Settings() {
  const user = useAppSelector(selectUser)
  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">Signed in as {user?.email}</p>
      </div>
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Team</h2>
        <RequirePermission
          permission="users:manage"
          fallback={<p className="text-sm text-muted-foreground">You don't have permission to invite users.</p>}
        >
          <InviteUserDialog />
        </RequirePermission>
      </section>
    </div>
  )
}
```

- [ ] **Step 3: Type-check + commit**

Run: `npx tsc -b` → Expected: PASS
```bash
git add src/components/users/InviteUserDialog.tsx src/pages/Settings.tsx
git commit -m "feat(auth): admin invite dialog gated by users:manage"
```

---

## Task 10: Idle config + session-start tracking + pure deadline helper

**Files:**
- Modify: `src/lib/auth/storage.ts`
- Create: `src/lib/auth/idle.ts`
- Test: `src/lib/auth/idle.test.ts`

**Interfaces:**
- Produces (storage): `getSessionStart(): number | null` (added); `setSession`/`clearSession` now also stamp/clear `vera.session_started_at`.
- Produces (idle): constants `IDLE_TIMEOUT_MS`, `WARNING_LEAD_MS`, `KEEPALIVE_THROTTLE_MS`, `ABSOLUTE_MAX_MS`; type `IdleState = { phase: "active" | "warning" | "expired"; secondsLeft: number; logoutAt: number }`; `computeIdleState(args): IdleState`.

- [ ] **Step 1: Add session-start to storage** (`src/lib/auth/storage.ts`)

Add the key + accessor and stamp it. Insert alongside the existing keys:
```ts
const SESSION_START_KEY = "vera.session_started_at"

export function getSessionStart(): number | null {
  const raw = sessionStorage.getItem(SESSION_START_KEY)
  return raw ? Number(raw) : null
}
```
In `setSession`, after writing token + slug, stamp the start (fresh each login):
```ts
sessionStorage.setItem(SESSION_START_KEY, String(Date.now()))
```
In `clearSession`, also remove it:
```ts
sessionStorage.removeItem(SESSION_START_KEY)
```

- [ ] **Step 2: Idle config + pure helper** (`src/lib/auth/idle.ts`)

```ts
// Idle auto-logout configuration + a pure deadline calculator. Kept side-effect free
// so it can be unit-tested with an injected `now` (no timers, no DOM).

export const IDLE_TIMEOUT_MS = 60 * 60 * 1000 // 60 min — backend idle TTL
export const WARNING_LEAD_MS = 60 * 1000 // warn 60s before logout
export const KEEPALIVE_THROTTLE_MS = 5 * 60 * 1000 // keepalive at most every 5 min
export const ABSOLUTE_MAX_MS = 12 * 60 * 60 * 1000 // 12h absolute session cap

export type IdlePhase = "active" | "warning" | "expired"
export type IdleState = { phase: IdlePhase; secondsLeft: number; logoutAt: number }

export function computeIdleState(args: {
  now: number
  lastActivity: number
  sessionStart: number
  idleTimeoutMs?: number
  warningLeadMs?: number
  absoluteMaxMs?: number
}): IdleState {
  const idleTimeout = args.idleTimeoutMs ?? IDLE_TIMEOUT_MS
  const warningLead = args.warningLeadMs ?? WARNING_LEAD_MS
  const absoluteMax = args.absoluteMaxMs ?? ABSOLUTE_MAX_MS

  const idleDeadline = args.lastActivity + idleTimeout
  const absoluteDeadline = args.sessionStart + absoluteMax
  // The absolute cap wins even under continuous activity.
  const logoutAt = Math.min(idleDeadline, absoluteDeadline)
  const remaining = logoutAt - args.now
  const secondsLeft = Math.max(0, Math.ceil(remaining / 1000))

  let phase: IdlePhase
  if (remaining <= 0) phase = "expired"
  else if (remaining <= warningLead) phase = "warning"
  else phase = "active"

  return { phase, secondsLeft, logoutAt }
}
```

- [ ] **Step 3: Write tests** (`src/lib/auth/idle.test.ts`)

```ts
import { describe, expect, it } from "vitest"
import { ABSOLUTE_MAX_MS, IDLE_TIMEOUT_MS, computeIdleState } from "@/lib/auth/idle"

const base = { lastActivity: 0, sessionStart: 0 }

describe("computeIdleState", () => {
  it("is active well within the idle window", () => {
    const s = computeIdleState({ ...base, now: 5 * 60 * 1000 })
    expect(s.phase).toBe("active")
  })

  it("enters warning 60s before idle logout", () => {
    const now = IDLE_TIMEOUT_MS - 30 * 1000 // 30s left
    const s = computeIdleState({ ...base, now })
    expect(s.phase).toBe("warning")
    expect(s.secondsLeft).toBe(30)
  })

  it("expires at the idle deadline", () => {
    const s = computeIdleState({ ...base, now: IDLE_TIMEOUT_MS + 1 })
    expect(s.phase).toBe("expired")
    expect(s.secondsLeft).toBe(0)
  })

  it("absolute cap forces logout despite recent activity", () => {
    // Active 1s ago, but the 12h cap is in the past → expired.
    const now = ABSOLUTE_MAX_MS + 1000
    const s = computeIdleState({ now, lastActivity: now - 1000, sessionStart: 0 })
    expect(s.phase).toBe("expired")
  })
})
```

- [ ] **Step 4: Run tests**

Run: `npm test -- src/lib/auth/idle.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/lib/auth/storage.ts src/lib/auth/idle.ts src/lib/auth/idle.test.ts
git commit -m "feat(auth): idle config, session-start tracking, deadline helper"
```

---

## Task 11: Idle manager + warning modal

**Files:**
- Create: `src/components/auth/IdleWarningDialog.tsx`
- Create: `src/components/auth/IdleManager.tsx`
- Modify: `src/components/layout/AppShell.tsx`

**Interfaces:**
- Consumes: `computeIdleState`, `KEEPALIVE_THROTTLE_MS` from `@/lib/auth/idle`; `getSessionStart` from storage; `keepaliveThunk`, `logoutThunk`, `selectTenantSlug` from slice.
- Produces: `<IdleManager />` (no props); `<IdleWarningDialog />` (props `{ open, secondsLeft, onStay, onLogout }`).

- [ ] **Step 1: Warning modal** (`src/components/auth/IdleWarningDialog.tsx`)

```tsx
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

export function IdleWarningDialog({
  open, secondsLeft, onStay, onLogout,
}: {
  open: boolean
  secondsLeft: number
  onStay: () => void
  onLogout: () => void
}) {
  return (
    <Dialog open={open}>
      <DialogContent
        showCloseButton={false}
        onEscapeKeyDown={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
        className="max-w-sm"
      >
        <DialogHeader>
          <DialogTitle>Still there?</DialogTitle>
          <DialogDescription>
            You'll be signed out due to inactivity in {secondsLeft}s.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onLogout}>Log out now</Button>
          <Button onClick={onStay}>Stay signed in</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 2: Idle manager** (`src/components/auth/IdleManager.tsx`)

```tsx
import { useCallback, useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { IdleWarningDialog } from "@/components/auth/IdleWarningDialog"
import { computeIdleState, KEEPALIVE_THROTTLE_MS } from "@/lib/auth/idle"
import { getSessionStart } from "@/lib/auth/storage"
import { keepaliveThunk, logoutThunk, selectTenantSlug } from "@/store/authSlice"
import { useAppDispatch, useAppSelector } from "@/store/hooks"

const ACTIVITY_EVENTS = ["mousemove", "mousedown", "keydown", "scroll", "touchstart"] as const
const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

// Single idle-manager. Mounted only inside AppShell (authenticated), so every
// listener/timer is torn down on logout via the effect cleanups when it unmounts.
export function IdleManager() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const slug = useAppSelector(selectTenantSlug) ?? DEFAULT_SLUG

  const lastActivity = useRef(Date.now())
  const lastKeepalive = useRef(Date.now())
  const warningRef = useRef(false) // mirror of `warning` for event handlers
  const loggingOut = useRef(false)

  const [warning, setWarning] = useState(false)
  const [secondsLeft, setSecondsLeft] = useState(0)

  const doLogout = useCallback(() => {
    if (loggingOut.current) return
    loggingOut.current = true
    void dispatch(logoutThunk()).finally(() => {
      navigate(`/tenants/${slug}/login`, { replace: true })
    })
  }, [dispatch, navigate, slug])

  const staySignedIn = useCallback(() => {
    const now = Date.now()
    lastActivity.current = now
    lastKeepalive.current = now
    warningRef.current = false
    setWarning(false)
    void dispatch(keepaliveThunk())
  }, [dispatch])

  // Activity → reset idle timer + throttled keepalive. Ignored while the warning
  // shows, so only the explicit "Stay signed in" can rescue the session.
  useEffect(() => {
    function onActivity() {
      if (warningRef.current) return
      const now = Date.now()
      lastActivity.current = now
      if (now - lastKeepalive.current >= KEEPALIVE_THROTTLE_MS) {
        lastKeepalive.current = now
        void dispatch(keepaliveThunk())
      }
    }
    ACTIVITY_EVENTS.forEach((e) => window.addEventListener(e, onActivity, { passive: true }))
    return () => ACTIVITY_EVENTS.forEach((e) => window.removeEventListener(e, onActivity))
  }, [dispatch])

  // 1s tick using real timestamps + immediate recompute on focus/visibility so a
  // backgrounded or slept tab logs out correctly on return.
  useEffect(() => {
    function check() {
      const sessionStart = getSessionStart() ?? Date.now()
      const state = computeIdleState({
        now: Date.now(),
        lastActivity: lastActivity.current,
        sessionStart,
      })
      if (state.phase === "expired") {
        doLogout()
        return
      }
      const show = state.phase === "warning"
      warningRef.current = show
      setWarning(show)
      setSecondsLeft(state.secondsLeft)
    }
    const id = window.setInterval(check, 1000)
    document.addEventListener("visibilitychange", check)
    window.addEventListener("focus", check)
    check()
    return () => {
      window.clearInterval(id)
      document.removeEventListener("visibilitychange", check)
      window.removeEventListener("focus", check)
    }
  }, [doLogout])

  return (
    <IdleWarningDialog
      open={warning}
      secondsLeft={secondsLeft}
      onStay={staySignedIn}
      onLogout={doLogout}
    />
  )
}
```

- [ ] **Step 3: Mount in AppShell** (`src/components/layout/AppShell.tsx`)

Add the import and render `<IdleManager />` once inside the shell's returned tree (e.g. just inside the outermost wrapper):
```tsx
import { IdleManager } from "@/components/auth/IdleManager"
// ...inside the returned JSX, near the top:
<IdleManager />
```

- [ ] **Step 4: Type-check + commit**

Run: `npx tsc -b` → Expected: PASS
```bash
git add src/components/auth/IdleManager.tsx src/components/auth/IdleWarningDialog.tsx src/components/layout/AppShell.tsx
git commit -m "feat(auth): idle auto-logout with pre-logout warning modal"
```

---

## Task 12: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Lint, type-check, test, build**

```bash
npm run lint
npx tsc -b
npm test
npm run build
```
Expected: all pass, no type errors.

- [ ] **Step 2: Manual E2E checklist** (`npm run dev`, backend running via `just api`)

Verify each:
- [ ] Login (no-MFA user) → lands in app; `/auth/me` populates user.
- [ ] Login (MFA-enrolled user) → MFA verify screen → TOTP code → app.
- [ ] MFA verify with a **recovery code** → app.
- [ ] Login (enforce-MFA, not enrolled) → enroll screen → QR scan → code → recovery codes shown → continue → app.
- [ ] Admin (`users:manage`) sees "Invite user" in Settings; sends invite; invite_url shown.
- [ ] Non-admin does NOT see the invite button (sees fallback text).
- [ ] Open invite_url → set password → (if enforce-MFA) enroll → recovery codes → redirected to login → sign in.
- [ ] Logout → redirected to tenant login; protected route now redirects to login.
- [ ] Manually expire/clear the session token in sessionStorage, hit a protected API → 401 → auto logout + redirect.
- [ ] **Idle:** temporarily set `IDLE_TIMEOUT_MS` to ~90s and `WARNING_LEAD_MS` to ~30s; confirm activity resets the timer and fires throttled keepalive; the warning appears with a live countdown; "Stay signed in" cancels + extends; inaction completes auto-logout + redirect. Restore the constants afterward.
- [ ] **Absolute cap:** temporarily set `ABSOLUTE_MAX_MS` to ~2 min; with continuous activity, confirm logout still fires at the cap. Restore afterward.
- [ ] **Backgrounded tab:** trigger the warning, switch tabs past the deadline, return → immediate logout (visibility/focus recompute).

- [ ] **Step 3: Final commit (if any cleanup)**

```bash
git add -A
git commit -m "chore(auth): verification pass cleanup"
```

---

## Self-Review Notes

- **Spec coverage:** Redux slice (T3) ✓; centralized API + 401/403 interceptor (T2) ✓; Login/MFA-verify/MFA-enroll/Invite screens (T6–T8) ✓; admin invite gated by `users:manage` (T9) ✓; `/auth/me` hydration (T3/T4) ✓; logout (T3/T8) ✓; route guard + RBAC UI (T4/T5) ✓; tenant slug in URL (T4) ✓; no password reset ✓.
- **Idle auto-logout coverage:** activity tracking + throttled keepalive (T11) ✓; idle threshold + 1-min warning modal with countdown (T10 helper + T11) ✓; "Stay signed in" resets + keepalive (T11) ✓; auto-logout + redirect on timeout (T11) ✓; absolute 12h cap via stored session-start (T10/T11) ✓; real-elapsed-time + visibility/focus recompute (T11) ✓; configurable constants (T10) ✓; teardown on unmount/logout (T11 effect cleanups) ✓; pure helper unit-tested with injected `now` (T10) ✓.
- **403 handling:** surfaced as `ApiError(403)` for callers; `RequirePermission` keeps gated UI from rendering so 403s are avoided proactively. Route-level access-denied can reuse `RequirePermission` with a fallback page if a permission-gated *route* is later added.
- **Type consistency:** `loginThunk` returns `LoginResult["mfa"]` and dispatches `setMfa`; screens read `selectMfa`. `enrollActivateThunk`/`activateInviteMfa` return `string[]` recovery codes. `setMfa` must be added to the slice `reducers` (see Task 3 + Task 6 notes).
