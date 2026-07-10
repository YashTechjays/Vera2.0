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
  createPromptDraft,
  getPromptSchema,
  previewPromptDocument,
  previewPromptVersion,
  type PromptDocument,
} from "./prompts"

const doc: PromptDocument = {
  kind: "prompt_document",
  session: { persona: "p", goal: "g", base_instructions: "b" },
  task_overrides: { wrap_up: { outro: "bye" } },
}

describe("prompts api client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("posts the document itself as the draft body, with the conventional Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await createPromptDraft("p1", doc)
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/versions", {
      method: "POST",
      body: doc,
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("fetches the published schema for a prompt", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await getPromptSchema("p1")
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/schema")
  })

  it("GET-previews a named version via query param, url-encoded", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await previewPromptVersion("p1", "v9")
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/preview?version_id=v9")
  })

  it("GET-previews the published version with no query param", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await previewPromptVersion("p1")
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/preview")
  })

  it("POST-previews an unsaved document", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ errors: [], rendered: {} })
    await previewPromptDocument("p1", doc)
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/preview", { method: "POST", body: doc })
  })
})
