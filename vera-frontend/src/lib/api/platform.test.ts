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
  deactivateOperator,
  inviteOperator,
  listOperators,
  resendOperatorInvitation,
  setTenantRetryConfig,
} from "./platform"

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

describe("platform api client — tenant retry config", () => {
  beforeEach(() => vi.resetAllMocks())

  it("sets a tenant's auto-retry config with an Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({
      tenant_id: "t1",
      auto_retry_enabled: true,
      retry_fill_threshold: 0.4,
    })
    await setTenantRetryConfig("t1", { auto_retry_enabled: true })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/retry-config", {
      method: "POST",
      body: { auto_retry_enabled: true },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("passes through a partial threshold-only patch", async () => {
    vi.mocked(apiRequest).mockResolvedValue({
      tenant_id: "t1",
      auto_retry_enabled: false,
      retry_fill_threshold: 0.4,
    })
    await setTenantRetryConfig("t1", { retry_fill_threshold: 0.4 })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/retry-config", {
      method: "POST",
      body: { retry_fill_threshold: 0.4 },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
})
