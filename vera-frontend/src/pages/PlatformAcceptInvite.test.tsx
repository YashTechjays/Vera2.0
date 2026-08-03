import { MemoryRouter } from "react-router-dom"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { PlatformAcceptInvite } from "@/pages/PlatformAcceptInvite"

describe("PlatformAcceptInvite", () => {
  it("renders the invalid-invitation state when no token is present", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/platform/accept-invite"]}>
        <PlatformAcceptInvite />
      </MemoryRouter>,
    )
    expect(html).toContain("Invalid invitation")
  })
})
