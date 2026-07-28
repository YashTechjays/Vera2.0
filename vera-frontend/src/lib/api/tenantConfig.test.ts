import { beforeEach, describe, expect, it, vi } from "vitest"

// Factory mock (not auto-mock): the real client imports auth/storage, which
// touches sessionStorage at module load — undefined in the node test env.
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
  return { apiRequest: vi.fn(), ApiError }
})

import { ApiError, apiRequest } from "@/lib/api/client"
import {
  getConcurrencyConfig,
  patchConcurrencyConfig,
  type ConcurrencyConfig,
} from "@/lib/api/tenantConfig"

describe("tenantConfig API client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("GETs the concurrency config", async () => {
    const config: ConcurrencyConfig = { max_agents_per_va: 3, max_concurrent_calls: 25 }
    vi.mocked(apiRequest).mockResolvedValue(config)

    await expect(getConcurrencyConfig()).resolves.toEqual(config)
    expect(apiRequest).toHaveBeenCalledWith("/tenant/config/concurrency")
  })

  it("PATCHes only the provided knobs", async () => {
    const config: ConcurrencyConfig = { max_agents_per_va: 5, max_concurrent_calls: 25 }
    vi.mocked(apiRequest).mockResolvedValue(config)

    await expect(patchConcurrencyConfig({ max_agents_per_va: 5 })).resolves.toEqual(config)
    expect(apiRequest).toHaveBeenCalledWith("/tenant/config/concurrency", {
      method: "PATCH",
      body: { max_agents_per_va: 5 },
    })
  })

  it("propagates ApiError when the caller lacks tenant:config:manage (403)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(403, "FORBIDDEN", "tenant:config:manage required"),
    )
    await expect(getConcurrencyConfig()).rejects.toBeInstanceOf(ApiError)
  })
})
