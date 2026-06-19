import { describe, expect, it } from "vitest"

import { validateAll, validateSection } from "./validation"

describe("validateAll", () => {
  it("flags an empty form's required fields as errors", () => {
    const errors = validateAll({})
    expect(errors["patient_information.patient_name"]).toBeTruthy()
    expect(errors["insurance_information.policy_number"]).toBeTruthy()
  })

  it("clears an error once the required field is filled", () => {
    const errors = validateAll({
      "patient_information.patient_name": "Sarah Johnson",
    })
    expect(errors["patient_information.patient_name"]).toBeUndefined()
  })

  it("does not flag optional fields", () => {
    const errors = validateAll({})
    expect(errors["patient_information.chart_number"]).toBeUndefined()
  })

  it("applies a conditional-required rule (spouse name when coverage is Family)", () => {
    const family = validateAll({ "benefit_coverage.coverage_type": "Family" })
    expect(family["patient_information.spouse_partner_name"]).toMatch(/required/i)

    const individual = validateAll({
      "benefit_coverage.coverage_type": "Individual",
    })
    expect(
      individual["patient_information.spouse_partner_name"]
    ).toBeUndefined()
  })
})

describe("validateSection", () => {
  it("returns only errors for the given section", () => {
    const errors = validateSection("insurance_information", {})
    expect(Object.keys(errors).length).toBeGreaterThan(0)
    expect(
      Object.keys(errors).every((p) => p.startsWith("insurance_information."))
    ).toBe(true)
    expect(errors["patient_information.patient_name"]).toBeUndefined()
  })
})
