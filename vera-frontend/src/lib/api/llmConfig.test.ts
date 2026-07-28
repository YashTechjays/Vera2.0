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
import { getLlmConfig, getLlmConfigHistory, resetLlmConfig, saveLlmConfig } from "./llmConfig"

describe("llmConfig api client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("fetches the current effective config", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await getLlmConfig()
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config")
  })

  it("fetches history", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await getLlmConfigHistory()
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config/history")
  })

  it("PUTs the model with no extra_config when omitted", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await saveLlmConfig("gemini-3.5-flash")
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config", {
      method: "PUT",
      body: { model: "gemini-3.5-flash", extra_config: null },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("PUTs the model with a thinking override when provided", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await saveLlmConfig("gemini-3.5-flash", { thinking_level: "high" })
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config", {
      method: "PUT",
      body: { model: "gemini-3.5-flash", extra_config: { thinking_level: "high" } },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("POSTs reset with the conventional Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await resetLlmConfig()
    expect(apiRequest).toHaveBeenCalledWith("/platform/llm-config/reset", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
})
