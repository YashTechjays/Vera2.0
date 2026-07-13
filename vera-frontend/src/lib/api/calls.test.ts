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

import { apiRequest, ApiError } from "@/lib/api/client"
import {
  endCall,
  getJoinToken,
  listCalls,
  publishCall,
  type CallSummary,
  type JoinTokenResponse,
} from "./calls"

const call: CallSummary = {
  id: "c1",
  tenant_id: "t1",
  status: "active",
  room_name: "call--t1--c1",
  patient_name: "Jane Doe",
  started_at: "2026-07-04T10:00:00Z",
  created_at: "2026-07-04T09:59:00Z",
  published: false,
  is_owner: true,
}

const joinToken: JoinTokenResponse = {
  token: "jwt",
  url: "ws://localhost:7880",
  room_name: "call--t1--c1",
}

describe("calls API client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("lists active calls with GET /calls", async () => {
    vi.mocked(apiRequest).mockResolvedValue([call])
    const out = await listCalls()
    expect(out).toEqual([call])
    expect(apiRequest).toHaveBeenCalledWith("/calls")
  })

  it("publishes a call (POST, no body)", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ ...call, published: true })
    const out = await publishCall("c1")
    expect(out.published).toBe(true)
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/publish", { method: "POST" })
  })

  it("gets a listen-only join token by default", async () => {
    vi.mocked(apiRequest).mockResolvedValue(joinToken)
    const out = await getJoinToken("c1")
    expect(out).toEqual(joinToken)
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/join-token")
  })

  it("requests a publishable token for intervene", async () => {
    vi.mocked(apiRequest).mockResolvedValue(joinToken)
    await getJoinToken("c1", true)
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/join-token?intervene=true")
  })

  it("ends a call (POST, no body)", async () => {
    vi.mocked(apiRequest).mockResolvedValue(null)
    await endCall("c1")
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/end", { method: "POST" })
  })

  it("propagates ApiError when intervene hits the single-intervener lock (409)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(409, "CONFLICT", "another supervisor is currently intervening on this call"),
    )
    await expect(getJoinToken("c1", true)).rejects.toBeInstanceOf(ApiError)
  })

  it("propagates ApiError when a non-owner tries to publish (403)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(403, "FORBIDDEN", "only the owner can publish"),
    )
    await expect(publishCall("c1")).rejects.toBeInstanceOf(ApiError)
  })

  it("propagates ApiError when ending an unknown call (404)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(new ApiError(404, "NOT_FOUND", "call not found"))
    await expect(endCall("c1")).rejects.toBeInstanceOf(ApiError)
  })

  it("propagates ApiError when joining a private call (404)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(new ApiError(404, "NOT_FOUND", "call not found"))
    await expect(getJoinToken("c1")).rejects.toBeInstanceOf(ApiError)
  })
})
