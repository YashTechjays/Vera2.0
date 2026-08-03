import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router-dom"

import { ForgotPassword } from "./ForgotPassword"

describe("ForgotPassword", () => {
  it("renders workspace and email fields with a submit button", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/forgot-password"]}>
        <ForgotPassword />
      </MemoryRouter>,
    )
    expect(html).toContain("Workspace")
    expect(html).toContain("Email")
    expect(html).toContain("Send reset link")
    expect(html).toContain("Back to sign in")
  })
})
