import { describe, expect, it } from "vitest"

import { valuesToIntakePayload } from "./intake"

describe("valuesToIntakePayload", () => {
  it("nests values by section, stripping the sections root", () => {
    expect(
      valuesToIntakePayload({
        "sections.patient_information.patient_name": "Jane Doe",
        "sections.patient_information.patient_dob": "1990-04-12",
        "sections.insurance_information.policy_number": "POL-1",
      }),
    ).toEqual({
      patient_information: { patient_name: "Jane Doe", patient_dob: "1990-04-12" },
      insurance_information: { policy_number: "POL-1" },
    })
  })

  it("recurses into nested groups", () => {
    expect(
      valuesToIntakePayload({
        "sections.benefits.ivf.cycle_limit": "3",
      }),
    ).toEqual({ benefits: { ivf: { cycle_limit: "3" } } })
  })

  it("skips empty and whitespace-only values, trimming the rest", () => {
    expect(
      valuesToIntakePayload({
        "sections.a.filled": "  x  ",
        "sections.a.blank": "   ",
        "sections.a.empty": "",
      }),
    ).toEqual({ a: { filled: "x" } })
  })

  it("ignores paths outside the sections namespace", () => {
    expect(valuesToIntakePayload({ stray: "x" })).toEqual({})
  })
})
