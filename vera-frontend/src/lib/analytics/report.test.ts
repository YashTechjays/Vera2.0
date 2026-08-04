import { describe, expect, it } from "vitest"

import { deltaPct, formatDuration, formatPct, presetRange } from "@/lib/analytics/report"

const NOW = new Date("2026-08-04T10:30:00.000Z") // a Tuesday

describe("presetRange", () => {
  it("last 7 days ends now and starts 7 days earlier", () => {
    const { date_from, date_to } = presetRange("7d", NOW)
    expect(date_to).toBe("2026-08-04T10:30:00.000Z")
    expect(date_from).toBe("2026-07-28T10:30:00.000Z")
  })

  it("this week starts on Monday 00:00 UTC", () => {
    expect(presetRange("week", NOW).date_from).toBe("2026-08-03T00:00:00.000Z")
  })

  it("this month starts on the 1st 00:00 UTC", () => {
    expect(presetRange("month", NOW).date_from).toBe("2026-08-01T00:00:00.000Z")
  })
})

describe("deltaPct", () => {
  it("computes the percent change vs the previous period", () => {
    expect(deltaPct(120, 100)).toBeCloseTo(20)
    expect(deltaPct(80, 100)).toBeCloseTo(-20)
  })

  it("is null when either side is missing or previous is zero", () => {
    expect(deltaPct(null, 100)).toBeNull()
    expect(deltaPct(100, null)).toBeNull()
    expect(deltaPct(100, 0)).toBeNull()
  })
})

describe("formatters", () => {
  it("formats seconds as m/s and handles null", () => {
    expect(formatDuration(360)).toBe("6m 0s")
    expect(formatDuration(null)).toBe("—")
  })

  it("formats percentages from fractions and from 0-100 values", () => {
    expect(formatPct(0.25, { fraction: true })).toBe("25.0%")
    expect(formatPct(53.333)).toBe("53.3%")
    expect(formatPct(null)).toBe("—")
  })
})
