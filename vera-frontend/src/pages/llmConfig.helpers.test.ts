import { describe, expect, it } from "vitest"
import { canReset, formatUpdatedAt, hasPendingChange } from "@/pages/llmConfig.helpers"
import type { LlmConfigState } from "@/lib/api/llmConfig"

const state = (overrides: Partial<LlmConfigState> = {}): LlmConfigState => ({
  provider: "google",
  model: "gemini-2.5-flash",
  is_default: false,
  created_at: "2026-07-23T10:00:00Z",
  created_by_user_id: "u1",
  ...overrides,
})

describe("hasPendingChange", () => {
  it("false when input matches the saved override", () => {
    expect(hasPendingChange("gemini-2.5-flash", state())).toBe(false)
  })
  it("true when input differs", () => {
    expect(hasPendingChange("gemini-3.5-flash", state())).toBe(true)
  })
  it("compares against empty string when at default (model is null)", () => {
    expect(hasPendingChange("", state({ model: null, is_default: true }))).toBe(false)
    expect(hasPendingChange("gemini-2.5-flash", state({ model: null, is_default: true }))).toBe(
      true,
    )
  })
})

describe("canReset", () => {
  it("false at default", () => {
    expect(canReset(state({ model: null, is_default: true }))).toBe(false)
  })
  it("true when overridden", () => {
    expect(canReset(state())).toBe(true)
  })
})

describe("formatUpdatedAt", () => {
  it("dash for null", () => {
    expect(formatUpdatedAt(null)).toBe("—")
  })
  it("formats a valid ISO date", () => {
    expect(formatUpdatedAt("2026-07-23T10:00:00Z")).not.toBe("—")
  })
  it("dash for an unparseable string", () => {
    expect(formatUpdatedAt("not-a-date")).toBe("—")
  })
})
