import { describe, expect, it } from "vitest"

import { evaluateCondition } from "./conditions"
import type { Condition } from "./types"

const COVERAGE = "sections.benefit_coverage.coverage_type"
const SPOUSE_GENDER = "sections.patient_information.spouse_gender"

describe("evaluateCondition — field ops", () => {
  it("eq matches the recorded value", () => {
    const c: Condition = { field: COVERAGE, op: "eq", value: "Family" }
    expect(evaluateCondition(c, { [COVERAGE]: "Family" })).toBe(true)
    expect(evaluateCondition(c, { [COVERAGE]: "Individual" })).toBe(false)
  })

  it("treats a missing value as empty: eq false, ne true", () => {
    expect(
      evaluateCondition({ field: COVERAGE, op: "eq", value: "Family" }, {})
    ).toBe(false)
    expect(
      evaluateCondition({ field: COVERAGE, op: "ne", value: "Family" }, {})
    ).toBe(true)
  })

  it("in / not_in take a list value", () => {
    const total = "sections.deductibles.individual.total"
    const skip: Condition = {
      field: total,
      op: "not_in",
      value: ["$0", "None", "No Deductible", "Unlimited", "No Limit"],
    }
    expect(evaluateCondition(skip, { [total]: "$1,500" })).toBe(true)
    expect(evaluateCondition(skip, { [total]: "No Limit" })).toBe(false)
    expect(
      evaluateCondition({ field: total, op: "in", value: ["$0", "None"] }, { [total]: "$0" })
    ).toBe(true)
  })
})

describe("evaluateCondition — combinators", () => {
  const family: Condition = { field: COVERAGE, op: "eq", value: "Family" }
  const male: Condition = { field: SPOUSE_GENDER, op: "eq", value: "Male" }

  it("all requires every member", () => {
    const c: Condition = { all: [family, male] }
    expect(
      evaluateCondition(c, { [COVERAGE]: "Family", [SPOUSE_GENDER]: "Male" })
    ).toBe(true)
    expect(evaluateCondition(c, { [COVERAGE]: "Family" })).toBe(false)
  })

  it("any requires at least one member", () => {
    const c: Condition = { any: [family, male] }
    expect(evaluateCondition(c, { [SPOUSE_GENDER]: "Male" })).toBe(true)
    expect(evaluateCondition(c, {})).toBe(false)
  })

  it("not negates", () => {
    expect(evaluateCondition({ not: family }, { [COVERAGE]: "Individual" })).toBe(true)
    expect(evaluateCondition({ not: family }, { [COVERAGE]: "Family" })).toBe(false)
  })
})

describe("evaluateCondition — shared_conditions refs", () => {
  const shared: Record<string, Condition> = {
    family_coverage: { field: COVERAGE, op: "eq", value: "Family" },
    male_partner_in_scope: {
      all: [
        { ref: "family_coverage" },
        { field: SPOUSE_GENDER, op: "eq", value: "Male" },
      ],
    },
  }

  it("resolves a ref, including refs nested inside a shared condition", () => {
    const c: Condition = { ref: "male_partner_in_scope" }
    expect(
      evaluateCondition(c, { [COVERAGE]: "Family", [SPOUSE_GENDER]: "Male" }, shared)
    ).toBe(true)
    expect(
      evaluateCondition(c, { [COVERAGE]: "Individual", [SPOUSE_GENDER]: "Male" }, shared)
    ).toBe(false)
  })

  it("evaluates an unknown ref as false", () => {
    expect(evaluateCondition({ ref: "nope" }, {}, shared)).toBe(false)
  })
})
