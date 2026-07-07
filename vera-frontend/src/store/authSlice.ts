import { createAsyncThunk, createSlice, type PayloadAction } from "@reduxjs/toolkit"

import * as authApi from "@/lib/auth/api"
import type { MeResponse } from "@/lib/auth/api"
import { apiErrorMessage, toApiErrorPayload, type ApiErrorPayload } from "@/lib/api/errors"
import { clearSession, getToken, setSession } from "@/lib/auth/storage"

type Status = "loading" | "anonymous" | "authenticated"
// `platform` marks a super-admin (platform-operator) challenge, so the verify step
// hits the slug-less /platform/auth/mfa/verify instead of the tenant route.
type MfaState = {
  token: string
  step: "verify" | "enroll"
  provisioningUri?: string
  platform?: boolean
}

type AuthState = {
  status: Status
  user: MeResponse | null
  tenantSlug: string | null
  mfa: MfaState | null
  loading: boolean
  error: string | null
  // Absolute session deadline (ms epoch), computed from /me's skew-safe
  // `login_absolute_remaining_seconds` at receipt. Null until /me hydrates. The idle
  // window comes straight off `user.login_idle_timeout_seconds`.
  sessionExpiresAt: number | null
}

const initialState: AuthState = {
  // A persisted token means we must hydrate via /me before we know who it is.
  status: getToken() ? "loading" : "anonymous",
  user: null,
  tenantSlug: null,
  mfa: null,
  loading: false,
  error: null,
  sessionExpiresAt: null,
}

// Reducers read the rejectWithValue payload; RTK's serialized error never
// carries backend detail, so anything else gets the fallback.
function message(payload: unknown, fallback: string): string {
  return apiErrorMessage(payload, fallback)
}

/** Run a thunk body, converting a thrown ApiError into a rejectWithValue
 *  payload so its message/status survive RTK's error serialization. */
async function runOrReject<T, R>(
  fn: () => Promise<T>,
  rejectWithValue: (value: ApiErrorPayload) => R,
): Promise<T | R> {
  try {
    return await fn()
  } catch (err) {
    const payload = toApiErrorPayload(err)
    if (payload) return rejectWithValue(payload)
    throw err
  }
}

export const fetchMe = createAsyncThunk("auth/fetchMe", async () => {
  const me = await authApi.getMe()
  // Convert the skew-safe remaining-seconds into an absolute deadline now, at receipt —
  // immune to client/server clock drift (the alternative, an absolute server timestamp,
  // would mis-time the warning under skew).
  return { me, sessionExpiresAt: Date.now() + me.login_absolute_remaining_seconds * 1000 }
})

export const loginThunk = createAsyncThunk<
  authApi.LoginResult["mfa"],
  { slug: string; email: string; password: string },
  { rejectValue: ApiErrorPayload }
>("auth/login", (arg, { dispatch, rejectWithValue }) =>
  runOrReject(async () => {
    // Remember the workspace so the MFA step (which runs before a session exists)
    // can read it from the store instead of the URL.
    dispatch(setTenantSlug(arg.slug))
    const res = await authApi.login(arg.slug, arg.email, arg.password)
    if (res.mfa === "none") {
      setSession(res.session_token ?? "", arg.slug)
      await dispatch(fetchMe()).unwrap()
    } else if (res.mfa === "verify") {
      dispatch(setMfa({ token: res.mfa_token ?? "", step: "verify" }))
    } else if (res.mfa === "enroll") {
      dispatch(
        setMfa({
          token: res.mfa_token ?? "",
          step: "enroll",
          provisioningUri: res.provisioning_uri ?? undefined,
        }),
      )
    }
    return res.mfa
  }, rejectWithValue),
)

export const verifyMfaThunk = createAsyncThunk<
  void,
  { slug: string; mfaToken: string; code: string },
  { rejectValue: ApiErrorPayload }
>("auth/verifyMfa", (arg, { dispatch, rejectWithValue }) =>
  runOrReject(async () => {
    const res = await authApi.verifyMfa(arg.slug, arg.mfaToken, arg.code)
    setSession(res.session_token, arg.slug)
    await dispatch(fetchMe()).unwrap()
  }, rejectWithValue),
)

// --- Platform operator (super admin) sign-in. No tenant slug; MFA is mandatory,
// so login never mints a session — it always hands back a verify challenge. ---
export const platformLoginThunk = createAsyncThunk<
  void,
  { email: string; password: string },
  { rejectValue: ApiErrorPayload }
>("auth/platformLogin", (arg, { dispatch, rejectWithValue }) =>
  runOrReject(async () => {
    const res = await authApi.platformLogin(arg.email, arg.password)
    dispatch(setMfa({ token: res.mfa_token ?? "", step: "verify", platform: true }))
  }, rejectWithValue),
)

export const platformVerifyMfaThunk = createAsyncThunk<
  void,
  { mfaToken: string; code: string },
  { rejectValue: ApiErrorPayload }
>("auth/platformVerifyMfa", (arg, { dispatch, rejectWithValue }) =>
  runOrReject(async () => {
    const res = await authApi.platformVerifyMfa(arg.mfaToken, arg.code)
    // Platform session belongs to no tenant — store with an empty slug; tenant_id
    // stays NULL and the idle manager's timeouts come from /me, not local storage.
    setSession(res.session_token, "")
    await dispatch(fetchMe()).unwrap()
  }, rejectWithValue),
)

export const enrollActivateThunk = createAsyncThunk<
  string[],
  { slug: string; mfaToken: string; code: string },
  { rejectValue: ApiErrorPayload }
>("auth/enrollActivate", (arg, { dispatch, rejectWithValue }) =>
  runOrReject(async () => {
    const res = await authApi.enrollActivate(arg.slug, arg.mfaToken, arg.code)
    setSession(res.session_token, arg.slug)
    await dispatch(fetchMe()).unwrap()
    return res.recovery_codes
  }, rejectWithValue),
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
      state.error = null
      state.sessionExpiresAt = null
    },
    clearError(state) {
      state.error = null
    },
    setMfa(state, action: PayloadAction<MfaState | null>) {
      state.mfa = action.payload
    },
    setTenantSlug(state, action: PayloadAction<string>) {
      state.tenantSlug = action.payload
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
      })
      .addCase(loginThunk.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.payload, "Invalid credentials.")
      })
      .addCase(fetchMe.pending, (s) => {
        if (s.status !== "authenticated") s.status = "loading"
      })
      .addCase(
        fetchMe.fulfilled,
        (s, a: PayloadAction<{ me: MeResponse; sessionExpiresAt: number }>) => {
          s.user = a.payload.me
          s.tenantSlug = a.payload.me.tenant_slug ?? s.tenantSlug
          s.sessionExpiresAt = a.payload.sessionExpiresAt
          s.status = "authenticated"
        },
      )
      .addCase(fetchMe.rejected, (s) => {
        clearSession()
        s.user = null
        s.sessionExpiresAt = null
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
        s.error = message(a.payload, "Verification failed.")
      })
      .addCase(platformLoginThunk.pending, (s) => {
        s.loading = true
        s.error = null
      })
      .addCase(platformLoginThunk.fulfilled, (s) => {
        s.loading = false
      })
      .addCase(platformLoginThunk.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.payload, "Invalid credentials.")
      })
      .addCase(platformVerifyMfaThunk.pending, (s) => {
        s.loading = true
        s.error = null
      })
      .addCase(platformVerifyMfaThunk.fulfilled, (s) => {
        s.loading = false
        s.mfa = null
      })
      .addCase(platformVerifyMfaThunk.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.payload, "Verification failed.")
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
        s.error = message(a.payload, "Enrollment failed.")
      })
      .addCase(logoutThunk.fulfilled, (s) => {
        clearSession()
        s.status = "anonymous"
        s.user = null
        s.tenantSlug = null
        s.mfa = null
        s.sessionExpiresAt = null
      })
  },
})

export const { forceLogout, clearError, setMfa, setTenantSlug } = authSlice.actions

export default authSlice.reducer

export const selectStatus = (s: { auth: AuthState }) => s.auth.status
export const selectUser = (s: { auth: AuthState }) => s.auth.user
export const selectTenantSlug = (s: { auth: AuthState }) => s.auth.tenantSlug
export const selectMfa = (s: { auth: AuthState }) => s.auth.mfa
export const selectAuthLoading = (s: { auth: AuthState }) => s.auth.loading
export const selectAuthError = (s: { auth: AuthState }) => s.auth.error
export const selectPermissions = (s: { auth: AuthState }) => s.auth.user?.permissions ?? []
// Backend-driven idle-manager config (from /me). `selectIdleTimeoutMs` is the idle
// window in ms; `selectSessionExpiresAt` is the absolute deadline (ms epoch) or null
// until /me hydrates. The IdleManager reads both instead of hardcoding constants.
export const selectIdleTimeoutMs = (s: { auth: AuthState }) =>
  s.auth.user != null ? s.auth.user.login_idle_timeout_seconds * 1000 : null
export const selectSessionExpiresAt = (s: { auth: AuthState }) => s.auth.sessionExpiresAt
// Super admins are platform-plane operators (account_type "platform"); they belong
// to no tenant and elevate into one. Used to gate platform-only UI.
export const selectIsSuperAdmin = (s: { auth: AuthState }) =>
  s.auth.user?.account_type === "platform"
export const selectActiveElevation = (s: { auth: AuthState }) =>
  s.auth.user?.active_elevation ?? null
// True while a platform operator holds an active elevation grant that hasn't
// expired — gates the tenant-scoped nav. The /auth/me snapshot can go stale
// between refreshes, so the expiry is checked here and AppShell re-fetches at
// expiry to re-sync the UI.
export const selectIsElevated = (s: { auth: AuthState }) => {
  const e = s.auth.user?.active_elevation
  return e != null && Date.parse(e.expires_at) > Date.now()
}
