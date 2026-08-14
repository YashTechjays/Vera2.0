import { describe, expect, it } from "vitest"

import {
  mockDisputes,
  humanizeLabel,
  confidenceLevel,
  confidenceHighlightClass,
  confidenceLabel,
  fieldConfidenceLevel,
  resolveConfidence,
  defaultFlags,
  activeDisputeValue,
  badgeValue,
  toggleApplied,
  toggleSwapped,
  applyFlagsForPaths,
  buildSavePayload,
  seedValues,
  type Dispute,
  type DisputeMap,
  type DisputeFlagMap,
} from "./disputes"

const d: Dispute = { previousValue: "No", currentValue: "Yes", confidence: 95 }

describe("humanizeLabel", () => {
  it("strips the sections. root and title-cases snake_case joined by ›", () => {
    expect(humanizeLabel("sections.insurance_information.plan_type")).toBe(
      "Insurance Information › Plan Type"
    )
  })
})

describe("confidenceLevel", () => {
  it("maps scores at documented thresholds", () => {
    // Each band's floor and the point just below it, so an off-by-one shift fails.
    expect(confidenceLevel(100)).toBe("high")
    expect(confidenceLevel(95)).toBe("high")
    expect(confidenceLevel(94)).toBe("medium")
    expect(confidenceLevel(85)).toBe("medium")
    expect(confidenceLevel(84)).toBe("low")
    expect(confidenceLevel(75)).toBe("low")
    expect(confidenceLevel(74)).toBe("very-low")
    expect(confidenceLevel(0)).toBe("very-low")
    expect(confidenceLevel(undefined)).toBe("unknown")
  })
})

describe("confidenceHighlightClass", () => {
  it("returns distinct classes per level", () => {
    expect(confidenceHighlightClass("high")).not.toBe(confidenceHighlightClass("very-low"))
    expect(confidenceHighlightClass("medium")).toBeTruthy()
  })
})

describe("resolveConfidence", () => {
  it("prefers the judge's verdict over the capture score", () => {
    expect(resolveConfidence(90, { confidence: 100, supported: true })).toEqual({
      score: 100,
      source: "judge",
      supported: true,
    })
  })

  it("falls back to the capture score when the judge has not run", () => {
    expect(resolveConfidence(90, null)).toEqual({
      score: 90,
      source: "captured",
      supported: true,
    })
  })
})

describe("fieldConfidenceLevel", () => {
  it("never lets a rejected value take a passing level", () => {
    expect(fieldConfidenceLevel(resolveConfidence(90, { confidence: 95, supported: false }))).toBe(
      "very-low"
    )
  })
})

describe("confidenceLabel", () => {
  it("names the pass that produced the number", () => {
    expect(confidenceLabel(resolveConfidence(90, { confidence: 100, supported: true }))).toBe(
      "judge 100% · high"
    )
    expect(confidenceLabel(resolveConfidence(90, null))).toBe("captured 90% · medium")
  })

  it("drops the number on an unsupported verdict", () => {
    expect(confidenceLabel(resolveConfidence(90, { confidence: 95, supported: false }))).toBe(
      "judge · unsupported"
    )
  })
})

describe("active/badge values + flags", () => {
  it("defaults: active = current, badge = previous", () => {
    const f = defaultFlags()
    expect(activeDisputeValue(d, f)).toBe("Yes")
    expect(badgeValue(d, f)).toBe("No")
  })

  it("swapped: active = previous, badge = current", () => {
    const f = toggleSwapped(defaultFlags())
    expect(activeDisputeValue(d, f)).toBe("No")
    expect(badgeValue(d, f)).toBe("Yes")
  })

  it("toggleApplied flips applied", () => {
    expect(toggleApplied(defaultFlags()).applied).toBe(true)
    expect(toggleApplied(toggleApplied(defaultFlags())).applied).toBe(false)
  })
})

describe("applyFlagsForPaths", () => {
  const flags: DisputeFlagMap = {
    "a.b": { applied: false, swapped: true },
  }
  const disputes: DisputeMap = {
    "a.b": { previousValue: "1", currentValue: "2" },
    "c.d": { previousValue: "3", currentValue: "4" },
  }

  it("marks every dispute path applied (and not swapped)", () => {
    const next = applyFlagsForPaths(disputes, flags, Object.keys(disputes))
    expect(next["a.b"].applied).toBe(true)
    expect(next["a.b"].swapped).toBe(false)
    expect(next["c.d"].applied).toBe(true)
  })

  it("touches only the given paths, ignoring ones with no dispute", () => {
    const next = applyFlagsForPaths(disputes, flags, ["a.b", "no.dispute"])
    expect(next["a.b"].applied).toBe(true)
    expect(next["c.d"]).toBeUndefined()
    expect(next["no.dispute"]).toBeUndefined()
  })
})

describe("buildSavePayload", () => {
  const disputes: DisputeMap = {
    "insurance_information.health_plan": {
      previousValue: "BCBS TX",
      currentValue: "Blue Cross Blue Shield",
    },
    "benefit_coverage.coverage_type": {
      previousValue: "Individual",
      currentValue: "Family",
    },
  }

  it("lists applied dispute paths", () => {
    const values = seedValues(disputes)
    const flags: DisputeFlagMap = {
      "insurance_information.health_plan": {
        applied: true,
        swapped: false,
      },
      "benefit_coverage.coverage_type": {
        applied: false,
        swapped: false,
      },
    }
    const payload = buildSavePayload(values, disputes, flags)
    expect(payload.disputeFields).toEqual(["insurance_information.health_plan"])
    expect(payload.formData["insurance_information.health_plan"]).toBe(
      "Blue Cross Blue Shield"
    )
  })
})

describe("mockDisputes integrity", () => {
  it("every dispute has two distinct values and a root-anchored path", () => {
    for (const [path, dd] of Object.entries(mockDisputes)) {
      expect(path).toMatch(/^sections(\.[a-z0-9_]+)+$/)
      expect(dd.previousValue).not.toBe(dd.currentValue)
    }
  })
})
