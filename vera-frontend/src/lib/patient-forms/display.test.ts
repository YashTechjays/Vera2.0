import { describe, expect, it } from "vitest"

import { allowedStatusTransitions, statusActionLabel, ageLabel } from "./display"

describe("allowedStatusTransitions", () => {
  it("offers (re)queue + complete from exception_review", () => {
    expect(allowedStatusTransitions("exception_review")).toEqual(["in_queue", "completed"])
  })

  it("offers queue from ready_for_processing and call_failed", () => {
    expect(allowedStatusTransitions("ready_for_processing")).toEqual(["in_queue"])
    expect(allowedStatusTransitions("call_failed")).toEqual(["in_queue"])
  })

  it("offers nothing for pipeline-driven / terminal states", () => {
    for (const s of ["in_queue", "in_call", "ai_processing", "completed"] as const) {
      expect(allowedStatusTransitions(s)).toEqual([])
    }
  })
})

describe("statusActionLabel", () => {
  it("labels the complete and queue actions", () => {
    expect(statusActionLabel("completed")).toBe("Mark complete")
    expect(statusActionLabel("in_queue")).toBe("Send to queue")
  })
})

describe("ageLabel", () => {
  it("renders day/hour/minute buckets", () => {
    const now = Date.now()
    expect(ageLabel(new Date(now - 3 * 864e5).toISOString())).toBe("3d")
    expect(ageLabel(new Date(now - 5 * 36e5).toISOString())).toBe("5h")
    expect(ageLabel(new Date(now - 12 * 6e4).toISOString())).toBe("12m")
  })
  it("dashes on null/invalid", () => {
    expect(ageLabel(null)).toBe("—")
    expect(ageLabel("not-a-date")).toBe("—")
  })
})
