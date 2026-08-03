import { configureStore } from "@reduxjs/toolkit"
import { renderToStaticMarkup } from "react-dom/server"
import { Provider } from "react-redux"
import { describe, expect, it } from "vitest"

import { InvitePlatformOperatorDialog } from "@/components/platform/InvitePlatformOperatorDialog"
import { PlatformOperators } from "@/pages/PlatformOperators"
import authReducer from "@/store/authSlice"

describe("InvitePlatformOperatorDialog", () => {
  it("renders an Invite operator trigger button", () => {
    const html = renderToStaticMarkup(<InvitePlatformOperatorDialog />)
    expect(html).toContain("Invite operator")
  })
})

// PlatformOperators reads Redux state via useAppSelector — there's no RTL and no
// existing convention for wrapping a connected page in a test, so this follows
// the same pattern authSlice.test.ts uses for reducer tests: a real
// configureStore() with just the auth reducer, seeded to its default
// (unauthenticated) state, wrapped in a real react-redux <Provider>.
describe("PlatformOperators", () => {
  it("renders the platform-only guard message for a non-super-admin", () => {
    const store = configureStore({ reducer: { auth: authReducer } })
    const html = renderToStaticMarkup(
      <Provider store={store}>
        <PlatformOperators />
      </Provider>
    )
    expect(html).toContain("only available to platform operators")
  })
})
