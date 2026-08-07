import { render, waitFor } from "@testing-library/react"
import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/api")

import { configureStore } from "@reduxjs/toolkit"
import * as api from "@/lib/auth/api"
import authReducer, { fetchMe } from "@/store/authSlice"
import { IdleManager } from "./IdleManager"

// Long timeouts so the 1s idle tick can never reach warning/logout inside a test.
const me: api.MeResponse = {
  user_id: "u1", email: "a@b.co", name: "A", account_type: "tenant",
  tenant_id: "t1", tenant_slug: "acme", roles: ["TENANT_ADMIN"],
  permissions: [], active_elevation: null,
  login_idle_timeout_seconds: 3600, login_absolute_remaining_seconds: 10 * 3600,
}

async function mountIdleManager() {
  vi.mocked(api.getMe).mockResolvedValue(me)
  const store = configureStore({ reducer: { auth: authReducer } })
  await store.dispatch(fetchMe())
  render(
    <Provider store={store}>
      <MemoryRouter>
        <IdleManager />
      </MemoryRouter>
    </Provider>,
  )
}

describe("IdleManager keepalive", () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it("sends a keepalive at mount so the server idle clock starts with the client's", async () => {
    vi.mocked(api.keepalive).mockResolvedValue({ expires_in_seconds: 900 })

    await mountIdleManager()

    await waitFor(() => expect(api.keepalive).toHaveBeenCalledTimes(1))
  })

  it("retries a failed keepalive on the next activity instead of going quiet for the throttle window", async () => {
    vi.mocked(api.keepalive)
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValue({ expires_in_seconds: 900 })

    await mountIdleManager()
    await waitFor(() => expect(api.keepalive).toHaveBeenCalledTimes(1))

    window.dispatchEvent(new Event("mousemove"))

    await waitFor(() => expect(api.keepalive).toHaveBeenCalledTimes(2))
  })

  it("does not resend within the throttle window after a successful keepalive", async () => {
    vi.mocked(api.keepalive).mockResolvedValue({ expires_in_seconds: 900 })

    await mountIdleManager()
    await waitFor(() => expect(api.keepalive).toHaveBeenCalledTimes(1))

    window.dispatchEvent(new Event("mousemove"))

    // Give any (wrong) extra dispatch a chance to land before asserting.
    await new Promise((r) => setTimeout(r, 25))
    expect(api.keepalive).toHaveBeenCalledTimes(1)
  })
})
