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
  selectIsElevated,
  selectIdleTimeoutMs,
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
})
