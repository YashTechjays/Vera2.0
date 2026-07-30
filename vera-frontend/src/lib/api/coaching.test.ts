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
import { sendCoachMessage, transcribeWhisper } from "./coaching"

describe("coaching API client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("sends a typed coaching message by default", async () => {
    vi.mocked(apiRequest).mockResolvedValue(null)
    await sendCoachMessage("c1", "ask about the deductible")
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/coach", {
      method: "POST",
      body: { message: "ask about the deductible", origin: "typed" },
    })
  })

  it("sends a whisper-origin coaching message when told to", async () => {
    vi.mocked(apiRequest).mockResolvedValue(null)
    await sendCoachMessage("c1", "mention the copay", "whisper")
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/coach", {
      method: "POST",
      body: { message: "mention the copay", origin: "whisper" },
    })
  })

  it("propagates ApiError when the shared coaching/whisper rate limit is hit (429)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(429, "RATE_LIMIT_EXCEEDED", "too many coaching/whisper actions on this call"),
    )
    await expect(sendCoachMessage("c1", "hi")).rejects.toBeInstanceOf(ApiError)
  })

  it("propagates ApiError when a non-owner without calls:intervene tries to coach (403)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(403, "FORBIDDEN", "missing permission calls:intervene"),
    )
    await expect(sendCoachMessage("c1", "hi")).rejects.toBeInstanceOf(ApiError)
  })

  it("uploads whisper audio as multipart form data and returns the transcribed text", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ text: "ask about the deductible" })
    const audio = new Blob(["fake-audio-bytes"], { type: "audio/webm" })

    const out = await transcribeWhisper("c1", audio)

    expect(out).toEqual({ text: "ask about the deductible" })
    expect(apiRequest).toHaveBeenCalledTimes(1)
    const [path, opts] = vi.mocked(apiRequest).mock.calls[0]
    expect(path).toBe("/calls/c1/on-demand-transcribe")
    expect(opts?.method).toBe("POST")
    expect(opts?.body).toBeInstanceOf(FormData)
    expect((opts?.body as FormData).get("audio")).toBeInstanceOf(Blob)
  })

  it("propagates ApiError when transcription is unavailable (503)", async () => {
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(503, "SERVICE_UNAVAILABLE", "transcription temporarily unavailable"),
    )
    const audio = new Blob(["fake-audio-bytes"], { type: "audio/webm" })
    await expect(transcribeWhisper("c1", audio)).rejects.toBeInstanceOf(ApiError)
  })
})
