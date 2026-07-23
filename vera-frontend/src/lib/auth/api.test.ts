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
import {
  platformAcceptInvite,
  platformActivateInviteMfa,
  platformValidateInvite,
  resendInvitation,
} from "./api"

describe("auth api client — resend and platform invite", () => {
  beforeEach(() => vi.resetAllMocks())

  it("resends a tenant invitation with the conventional Idempotency-Key", async () => {
    const mockResult = {
      user_id: "u1",
      email: "a@b.com",
      invite_url: "http://localhost:5173/invite/token-123",
      email_sent: true,
    }
    vi.mocked(apiRequest).mockResolvedValue(mockResult)
    const result = await resendInvitation("user-1")
    expect(apiRequest).toHaveBeenCalledWith("/users/user-1/resend-invitation", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
    expect(result).toEqual(mockResult)
  })

  it("validates a platform invite with no tenant slug", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ state: "valid" })
    await platformValidateInvite("tok123")
    expect(apiRequest).toHaveBeenCalledWith(
      "/platform/auth/invitations/validate?token=tok123",
      { method: "GET", auth: false },
    )
  })

  it("accepts a platform invite", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ mfa_required: true })
    await platformAcceptInvite("tok123", "a-strong-password")
    expect(apiRequest).toHaveBeenCalledWith("/platform/auth/invitations/accept", {
      method: "POST",
      body: { token: "tok123", password: "a-strong-password" },
      auth: false,
    })
  })

  it("activates platform invite MFA", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await platformActivateInviteMfa("mfa-tok", "123456")
    expect(apiRequest).toHaveBeenCalledWith("/platform/auth/invitations/activate-mfa", {
      method: "POST",
      body: { mfa_token: "mfa-tok", code: "123456" },
      auth: false,
    })
  })
})
