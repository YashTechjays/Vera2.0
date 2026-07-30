import { describe, expect, it } from "vitest"

import { healthDisplay, healthTone, isHealthStale } from "@/lib/monitoring/health"

describe("healthTone", () => {
  it("buckets scores", () => {
    expect(healthTone(null)).toBe("unknown")
    expect(healthTone(85)).toBe("good")
    expect(healthTone(70)).toBe("good")
    expect(healthTone(69)).toBe("warn")
    expect(healthTone(40)).toBe("warn")
    expect(healthTone(39)).toBe("bad")
  })
})

describe("isHealthStale", () => {
  it("is stale past 3x the analysis interval (45s), never for unassessed", () => {
    const now = Date.parse("2026-07-17T10:01:00Z")
    expect(isHealthStale(null, now)).toBe(false)
    expect(isHealthStale("2026-07-17T10:00:30Z", now)).toBe(false) // 30s
    expect(isHealthStale("2026-07-17T10:00:00Z", now)).toBe(true) // 60s
  })
})

describe("healthDisplay", () => {
  it("renders assessing / score / stale states", () => {
    const now = Date.parse("2026-07-17T10:01:00Z")
    expect(healthDisplay(null, null, now)).toEqual({
      text: "Assessing…",
      tone: "unknown",
      stale: false,
    })
    expect(healthDisplay(82, "2026-07-17T10:00:50Z", now)).toEqual({
      text: "82%",
      tone: "good",
      stale: false,
    })
    expect(healthDisplay(82, "2026-07-17T10:00:00Z", now).stale).toBe(true)
  })
})
