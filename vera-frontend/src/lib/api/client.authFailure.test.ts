import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({ getToken: vi.fn() }))

import * as storage from "@/lib/auth/storage"
import { apiRequest, registerAuthFailureHandler } from "./client"

function fail401(): Response {
  return new Response(
    JSON.stringify({ status: "FAIL", error_code: "UNAUTHORIZED", message: "session expired" }),
    { status: 401, headers: { "Content-Type": "application/json" } },
  )
}

// VR2-202: an in-flight request sent with the PREVIOUS session's token can land
// its 401 after a new login — it must not force-logout the fresh session.
describe("apiRequest 401 handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("ignores a 401 from a request sent with a superseded token", async () => {
    const handler = vi.fn()
    registerAuthFailureHandler(handler)
    vi.mocked(storage.getToken)
      .mockReturnValueOnce("old-token") // read at send time
      .mockReturnValue("new-token") // read again when the 401 lands
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(fail401()))

    await expect(apiRequest("/auth/me")).rejects.toThrow("session expired")
    expect(handler).not.toHaveBeenCalled()
  })

  it("clears auth state when the 401 belongs to the current token", async () => {
    const handler = vi.fn()
    registerAuthFailureHandler(handler)
    vi.mocked(storage.getToken).mockReturnValue("tok")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(fail401()))

    await expect(apiRequest("/auth/me")).rejects.toThrow("session expired")
    expect(handler).toHaveBeenCalledTimes(1)
  })
})
