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
  createTenant,
  deactivateOperator,
  deactivateTenant,
  getTenant,
  inviteOperator,
  inviteTenantUser,
  listOperators,
  listTenantRoles,
  listTenants,
  listTenantUsers,
  reactivateTenant,
  resendOperatorInvitation,
  setTenantRetryConfig,
  updateTenant,
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

describe("platform api client — tenant CRUD (VR2-30)", () => {
  beforeEach(() => vi.resetAllMocks())

  it("lists tenants without a status filter by default", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listTenants()
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants")
  })

  it("lists tenants filtered by status", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listTenants({ status: "deactivated" })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants?status=deactivated")
  })

  it("asks for every status explicitly — the default is active-only", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listTenants({ status: "all" })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants?status=all")
  })

  it("creates a tenant with an Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await createTenant({ name: "Acme Health", slug: "acme", region: "us-east" })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants", {
      method: "POST",
      body: { name: "Acme Health", slug: "acme", region: "us-east" },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("reads one tenant's detail", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await getTenant("t1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1")
  })

  it("updates only the fields it is given", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await updateTenant("t1", { name: "Renamed", max_retries: 2 })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1", {
      method: "PATCH",
      body: { name: "Renamed", max_retries: 2 },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("deactivates a tenant", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await deactivateTenant("t1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/deactivate", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("reactivates a tenant", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await reactivateTenant("t1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/reactivate", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("lists a tenant's users", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listTenantUsers("t1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/users")
  })

  it("invites a user into a tenant with an Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await inviteTenantUser("t1", {
      email: "a@b.com",
      name: "A",
      roleIds: ["r1"],
      sendEmail: true,
    })
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/users/invitations", {
      method: "POST",
      body: { email: "a@b.com", name: "A", role_ids: ["r1"], send_email: true },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("lists the roles assignable inside a tenant", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listTenantRoles("t1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/t1/roles")
  })

  it("url-encodes the tenant id in every path", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await getTenant("a/b")
    expect(apiRequest).toHaveBeenCalledWith("/platform/tenants/a%2Fb")
  })
})
