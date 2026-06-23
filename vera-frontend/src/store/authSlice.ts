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
      dispatch(setMfa({ token: res.mfa_token ?? "", step: "enroll", provisioningUri: res.provisioning_uri ?? undefined }))
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
      state.error = null
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

export const { forceLogout, clearError, setMfa, setTenantSlug } = authSlice.actions

export default authSlice.reducer

export const selectStatus = (s: { auth: AuthState }) => s.auth.status
export const selectUser = (s: { auth: AuthState }) => s.auth.user
export const selectTenantSlug = (s: { auth: AuthState }) => s.auth.tenantSlug
export const selectMfa = (s: { auth: AuthState }) => s.auth.mfa
export const selectAuthLoading = (s: { auth: AuthState }) => s.auth.loading
export const selectAuthError = (s: { auth: AuthState }) => s.auth.error
export const selectPermissions = (s: { auth: AuthState }) => s.auth.user?.permissions ?? []
