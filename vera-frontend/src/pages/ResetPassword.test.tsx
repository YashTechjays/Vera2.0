import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router-dom"

import { ResetPassword } from "./ResetPassword"

describe("ResetPassword", () => {
  it("renders the invalid card when the link carries no token", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/tenants/acme/reset-password"]}>
        <ResetPassword />
      </MemoryRouter>,
    )
    expect(html).toContain("Invalid reset link")
    expect(html).toContain("Go to sign in")
  })

  it("shows the checking state while a token is being validated", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/tenants/acme/reset-password?token=tok123"]}>
        <ResetPassword />
      </MemoryRouter>,
    )
    expect(html).toContain("Checking reset link")
  })
})
