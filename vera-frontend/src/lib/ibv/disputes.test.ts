import { describe, expect, it } from "vitest"

import {
  mockDisputes,
  humanizeLabel,
  confidenceLevel,
  confidenceHighlightClass,
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
    expect(confidenceLevel(100)).toBe("high")
    expect(confidenceLevel(95)).toBe("medium")
    expect(confidenceLevel(85)).toBe("low")
    expect(confidenceLevel(50)).toBe("very-low")
    expect(confidenceLevel(undefined)).toBe("unknown")
  })
})

describe("confidenceHighlightClass", () => {
  it("returns distinct classes per level", () => {
    expect(confidenceHighlightClass(100)).not.toBe(confidenceHighlightClass(50))
    expect(confidenceHighlightClass(95)).toBeTruthy()
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

  it("marks every dispute path applied, preserving an existing swap", () => {
    const next = applyFlagsForPaths(disputes, flags, Object.keys(disputes))
    expect(next["a.b"]).toEqual({ applied: true, swapped: true })
    expect(next["c.d"]).toEqual({ applied: true, swapped: false })
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
