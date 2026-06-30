import { describe, expect, it } from "vitest"
import { pickInitialVersion } from "@/pages/agentPrompt.helpers"
import type { PromptVersionSummary } from "@/lib/api/prompts"

const v = (version: number, status: string): PromptVersionSummary => ({
  id: `id-${version}`, version, status, created_at: "2026-06-29T00:00:00Z",
})

describe("pickInitialVersion", () => {
  it("returns undefined for no versions", () => {
    expect(pickInitialVersion([])).toBeUndefined()
  })
  it("prefers the published version", () => {
    expect(pickInitialVersion([v(2, "draft"), v(1, "published")])?.version).toBe(1)
  })
  it("falls back to the first (newest) when none published", () => {
    expect(pickInitialVersion([v(3, "draft"), v(2, "draft")])?.version).toBe(3)
  })
})
