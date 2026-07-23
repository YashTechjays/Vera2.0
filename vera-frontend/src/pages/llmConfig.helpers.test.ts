import { describe, expect, it } from "vitest"
import { THINKING_LEVELS, canReset, formatUpdatedAt, hasPendingChange, isGemini3Model } from "@/pages/llmConfig.helpers"
import type { LlmConfigState } from "@/lib/api/llmConfig"

const state = (overrides: Partial<LlmConfigState> = {}): LlmConfigState => ({
  provider: "google",
  model: "gemini-2.5-flash",
  extra_config: null,
  is_default: false,
  created_at: "2026-07-23T10:00:00Z",
  created_by_user_id: "u1",
  ...overrides,
})

describe("hasPendingChange", () => {
  it("false when input matches the saved override and extra_config is unchanged", () => {
    expect(hasPendingChange("gemini-2.5-flash", null, state())).toBe(false)
  })
  it("true when model input differs", () => {
    expect(hasPendingChange("gemini-3.5-flash", null, state())).toBe(true)
  })
  it("compares against empty string when at default (model is null)", () => {
    expect(hasPendingChange("", null, state({ model: null, is_default: true }))).toBe(false)
    expect(
      hasPendingChange("gemini-2.5-flash", null, state({ model: null, is_default: true })),
    ).toBe(true)
  })
  it("true when extra_config differs even if model is unchanged", () => {
    expect(
      hasPendingChange("gemini-2.5-flash", { thinking_budget: 500 }, state()),
    ).toBe(true)
  })
  it("false when extra_config matches the saved value", () => {
    const saved = state({ extra_config: { thinking_budget: 0 } })
    expect(hasPendingChange("gemini-2.5-flash", { thinking_budget: 0 }, saved)).toBe(false)
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

describe("isGemini3Model", () => {
  it("true for suggested Gemini 3 names", () => {
    expect(isGemini3Model("gemini-3.1-flash-lite")).toBe(true)
    expect(isGemini3Model("gemini-3.5-flash")).toBe(true)
    expect(isGemini3Model("GEMINI-3-PRO")).toBe(true)
  })
  it("false for pre-3 names", () => {
    expect(isGemini3Model("gemini-2.5-flash")).toBe(false)
  })
})

describe("THINKING_LEVELS", () => {
  it("is the full four-value enum, in order", () => {
    expect(THINKING_LEVELS).toEqual(["minimal", "low", "medium", "high"])
  })
})
