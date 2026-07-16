import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/api")
vi.mock("@/lib/auth/storage", () => ({
  getToken: () => null,
  setSession: vi.fn(),
  clearSession: vi.fn(),
  getAuthPlane: vi.fn(() => null),
  setAuthPlane: vi.fn(),
}))

import { configureStore } from "@reduxjs/toolkit"
import * as api from "@/lib/auth/api"
import * as storage from "@/lib/auth/storage"
import authReducer, {
  forceLogout,
  loginThunk,
  loginRedirectPath,
  logoutThunk,
  platformLoginThunk,
  platformEnrollActivateThunk,
  fetchMe,
  selectStatus,
  selectMfa,
  selectIsElevated,
  selectIdleTimeoutMs,
  selectLogoutRedirectPath,
  selectSessionExpiresAt,
} from "@/store/authSlice"

function makeStore() {
  return configureStore({ reducer: { auth: authReducer } })
}

const me: api.MeResponse = {
  user_id: "u1", email: "a@b.co", name: "A", account_type: "tenant",
  tenant_id: "t1", tenant_slug: "acme", roles: ["TENANT_ADMIN"],
  permissions: ["users:manage"], active_elevation: null,
  login_idle_timeout_seconds: 3600, login_absolute_remaining_seconds: 10 * 3600,
}

const platformMe: api.MeResponse = {
  ...me, account_type: "platform", tenant_id: null, tenant_slug: null,
  roles: ["SUPER_ADMIN"],
}

const elevatedState = (expiresAt: string | null) =>
  ({
    auth: {
      user: expiresAt
        ? { ...me, active_elevation: { target_tenant_id: "t1", expires_at: expiresAt } }
        : me,
    },
  }) as Parameters<typeof selectIsElevated>[0]

describe("authSlice", () => {
  beforeEach(() => vi.resetAllMocks())

  it("selectIsElevated honors grant presence and expiry", () => {
    expect(selectIsElevated(elevatedState(null))).toBe(false)
    expect(selectIsElevated(elevatedState(new Date(Date.now() + 60_000).toISOString()))).toBe(true)
    expect(selectIsElevated(elevatedState(new Date(Date.now() - 60_000).toISOString()))).toBe(false)
  })

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

  it("platform login with enroll shows the QR (enroll step + provisioning uri)", async () => {
    vi.mocked(api.platformLogin).mockResolvedValue({
      mfa: "enroll",
      session_token: null,
      mfa_token: "et",
      provisioning_uri: "otpauth://totp/Vera:ops?secret=ABC",
    })
    const store = makeStore()
    const step = await store.dispatch(
      platformLoginThunk({ email: "ops@vera.example", password: "x" }),
    ).unwrap()
    expect(step).toBe("enroll")
    const mfa = selectMfa(store.getState())
    expect(mfa?.step).toBe("enroll")
    expect(mfa?.platform).toBe(true)
    expect(mfa?.provisioningUri).toBe("otpauth://totp/Vera:ops?secret=ABC")
    expect(selectStatus(store.getState())).toBe("anonymous")
  })

  it("platform enroll-activate mints the session and clears mfa (no recovery codes)", async () => {
    vi.mocked(api.platformEnrollActivate).mockResolvedValue({ session_token: "tok" })
    vi.mocked(api.getMe).mockResolvedValue({
      ...me, account_type: "platform", tenant_id: null, tenant_slug: null,
    })
    const store = makeStore()
    await store.dispatch(
      platformEnrollActivateThunk({ mfaToken: "et", code: "123456" }),
    ).unwrap()
    expect(selectStatus(store.getState())).toBe("authenticated")
    expect(selectMfa(store.getState())).toBeNull()
  })

  it("platform login with verify goes to the verify step", async () => {
    vi.mocked(api.platformLogin).mockResolvedValue({
      mfa: "verify", session_token: null, mfa_token: "mt", provisioning_uri: null,
    })
    const store = makeStore()
    const step = await store.dispatch(
      platformLoginThunk({ email: "ops@vera.example", password: "x" }),
    ).unwrap()
    expect(step).toBe("verify")
    expect(selectMfa(store.getState())?.step).toBe("verify")
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
    expect(selectSessionExpiresAt(store.getState())).toBeNull()
  })

  it("fetchMe exposes backend-driven idle config and an absolute deadline", async () => {
    vi.mocked(api.getMe).mockResolvedValue(me)
    const store = makeStore()
    const before = Date.now()
    await store.dispatch(fetchMe())
    const after = Date.now()

    expect(selectIdleTimeoutMs(store.getState())).toBe(3600 * 1000)
    // Deadline is computed at receipt from the skew-safe remaining seconds.
    const deadline = selectSessionExpiresAt(store.getState())
    expect(deadline).not.toBeNull()
    expect(deadline!).toBeGreaterThanOrEqual(before + 10 * 3600 * 1000)
    expect(deadline!).toBeLessThanOrEqual(after + 10 * 3600 * 1000)
  })

  it("captures the platform plane on logout so the redirect path is /platform/login", async () => {
    vi.mocked(api.getMe).mockResolvedValue(platformMe)
    vi.mocked(api.logout).mockResolvedValue(null)
    const store = makeStore()
    await store.dispatch(fetchMe())
    // Pre-logout the selector still answers /login (logoutPlane unset, persisted
    // hint cleared once the MFA challenge completed) — which is exactly why logout
    // consumers must read the path AFTER the logout reducers run, never from a
    // value captured at render time.
    expect(selectLogoutRedirectPath(store.getState())).toBe("/login")
    await store.dispatch(logoutThunk())
    expect(selectLogoutRedirectPath(store.getState())).toBe("/platform/login")
  })

  it("captures the platform plane on forceLogout (401 burst) as well", async () => {
    vi.mocked(api.getMe).mockResolvedValue(platformMe)
    const store = makeStore()
    await store.dispatch(fetchMe())
    store.dispatch(forceLogout())
    expect(selectLogoutRedirectPath(store.getState())).toBe("/platform/login")
    // A second forceLogout (no user left) must not overwrite the captured plane.
    store.dispatch(forceLogout())
    expect(selectLogoutRedirectPath(store.getState())).toBe("/platform/login")
  })

  it("loginRedirectPath is platform-aware from state, else the persisted plane", () => {
    // In-memory challenge → trust its plane flag.
    expect(loginRedirectPath({ platform: true })).toBe("/platform/login")
    expect(loginRedirectPath({ platform: false })).toBe("/login")
    // No challenge (refresh) → fall back to the persisted hint.
    vi.mocked(storage.getAuthPlane).mockReturnValueOnce("platform")
    expect(loginRedirectPath(null)).toBe("/platform/login")
    vi.mocked(storage.getAuthPlane).mockReturnValueOnce(null)
    expect(loginRedirectPath(null)).toBe("/login")
  })
})
