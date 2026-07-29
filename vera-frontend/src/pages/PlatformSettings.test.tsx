import { configureStore } from "@reduxjs/toolkit"
import { renderToStaticMarkup } from "react-dom/server"
import { Provider } from "react-redux"
import { describe, expect, it, vi } from "vitest"

import type { MeResponse } from "@/lib/auth/api"
import { PlatformSettings } from "@/pages/PlatformSettings"
import authReducer from "@/store/authSlice"

// The page fetches on mount for a permitted caller; renderToStaticMarkup never flushes
// effects, but stub the module anyway so a future switch to a flushing renderer can't
// start hitting the network from a unit test.
vi.mock("@/lib/api/platform", () => ({
  listTenants: vi.fn(() => new Promise(() => {})),
  setTenantObserverEnabled: vi.fn(),
}))

const operator: MeResponse = {
  user_id: "u1",
  email: "op@vera.example",
  name: "Op",
  account_type: "platform",
  tenant_id: null,
  tenant_slug: null,
  roles: ["SUPER_ADMIN"],
  permissions: [],
  active_elevation: null,
  login_idle_timeout_seconds: 3600,
  login_absolute_remaining_seconds: 10 * 3600,
}

// Same connected-page pattern as PlatformOperators.test.tsx: a real store with just the
// auth reducer, preloaded, wrapped in a real <Provider>.
function render(permissions: string[]) {
  const store = configureStore({
    reducer: { auth: authReducer },
    preloadedState: {
      auth: {
        ...authReducer(undefined, { type: "@@INIT" }),
        status: "authenticated" as const,
        user: { ...operator, permissions },
      },
    },
  })
  return renderToStaticMarkup(
    <Provider store={store}>
      <PlatformSettings />
    </Provider>
  )
}

describe("PlatformSettings", () => {
  it("renders the management table for a caller holding platform:tenants:manage", () => {
    const html = render(["platform:tenants:manage"])
    expect(html).toContain("Toggle AI form filling per tenant")
  })

  // The page used to gate on account_type === "platform", so any platform operator got
  // the table and their toggles then 403'd from the backend.
  it("withholds the surface from a platform operator lacking the permission", () => {
    const html = render(["platform:elevations:read"])
    expect(html).toContain("do not have permission")
    expect(html).not.toContain("Toggle AI form filling per tenant")
  })
})
