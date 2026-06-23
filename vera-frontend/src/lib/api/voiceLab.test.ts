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
import { endVoiceSession, startVoiceSession, type VoiceSessionResponse } from "./voiceLab"

const response: VoiceSessionResponse = {
  room_name: "call--t--c",
  url: "ws://localhost:7880",
  token: "jwt",
  mode: "browser",
}

describe("startVoiceSession", () => {
  beforeEach(() => vi.resetAllMocks())

  it("posts a browser session with no phone number", async () => {
    vi.mocked(apiRequest).mockResolvedValue(response)
    const out = await startVoiceSession({ mode: "browser" })
    expect(out).toEqual(response)
    expect(apiRequest).toHaveBeenCalledWith("/voice-lab/sessions", {
      method: "POST",
      body: { mode: "browser" },
    })
  })

  it("posts an outbound session with the phone number", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ ...response, mode: "outbound" })
    await startVoiceSession({ mode: "outbound", phone_number: "+15551234567" })
    expect(apiRequest).toHaveBeenCalledWith("/voice-lab/sessions", {
      method: "POST",
      body: { mode: "outbound", phone_number: "+15551234567" },
    })
  })

  it("propagates ApiError (e.g. outbound not configured)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(409, "CONFLICT", "outbound SIP is not configured"),
    )
    await expect(startVoiceSession({ mode: "outbound" })).rejects.toBeInstanceOf(ApiError)
  })

  it("ends a session by DELETEing the room (url-encoded)", async () => {
    vi.mocked(apiRequest).mockResolvedValue(null)
    await endVoiceSession("call--t--c")
    expect(apiRequest).toHaveBeenCalledWith("/voice-lab/sessions/call--t--c", {
      method: "DELETE",
    })
  })
})
