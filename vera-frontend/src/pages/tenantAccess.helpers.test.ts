import { describe, expect, it } from "vitest"
import { MAX_ELEVATION_MINUTES } from "@/lib/api/platform"
import { parseDurationMinutes } from "@/pages/tenantAccess.helpers"

describe("parseDurationMinutes", () => {
  it("accepts whole minutes within range", () => {
    expect(parseDurationMinutes("1")).toBe(1)
    expect(parseDurationMinutes("60")).toBe(60)
    expect(parseDurationMinutes(String(MAX_ELEVATION_MINUTES))).toBe(MAX_ELEVATION_MINUTES)
  })
  it("accepts a leading zero left over from mid-edit typing", () => {
    expect(parseDurationMinutes("060")).toBe(60)
  })
  it("rejects empty and whitespace (a cleared field)", () => {
    expect(parseDurationMinutes("")).toBeNull()
    expect(parseDurationMinutes("  ")).toBeNull()
  })
  it("rejects out-of-range and non-whole values", () => {
    expect(parseDurationMinutes("0")).toBeNull()
    expect(parseDurationMinutes("-5")).toBeNull()
    expect(parseDurationMinutes(String(MAX_ELEVATION_MINUTES + 1))).toBeNull()
    expect(parseDurationMinutes("4.5")).toBeNull()
  })
})
