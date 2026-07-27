import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    httpStatus: number
    errorCode: string | null
    constructor(httpStatus: number, errorCode: string | null, message: string) {
      super(message)
      this.name = "ApiError"
      this.httpStatus = httpStatus
      this.errorCode = errorCode
    }
  }
  return { apiRequest: vi.fn(), ApiError, randomId: () => "test-idempotency-key" }
})

import { apiRequest } from "@/lib/api/client"
import { deactivateOperator, inviteOperator, listOperators, resendOperatorInvitation } from "./platform"

describe("platform api client — operators", () => {
  beforeEach(() => vi.resetAllMocks())

  it("lists platform operators", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listOperators()
    expect(apiRequest).toHaveBeenCalledWith("/platform/users")
  })

  it("invites a platform operator with an Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await inviteOperator({ email: "a@b.com", name: "A", sendEmail: true })
    expect(apiRequest).toHaveBeenCalledWith("/platform/users/invitations", {
      method: "POST",
      body: { email: "a@b.com", name: "A", send_email: true },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("deactivates a platform operator", async () => {
    vi.mocked(apiRequest).mockResolvedValue(null)
    await deactivateOperator("op-1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/users/op-1/deactivate", { method: "POST" })
  })

  it("resends a platform operator invitation", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await resendOperatorInvitation("op-1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/users/op-1/resend-invitation", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
})
