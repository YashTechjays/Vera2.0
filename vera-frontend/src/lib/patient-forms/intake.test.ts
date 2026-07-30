import { describe, expect, it } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { parseSchema } from "@/lib/ibv/schema"
import { applicableValues, valuesToIntakePayload } from "./intake"

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

describe("applicableValues", () => {
  const schema = parseSchema(rawSchema)
  // spouse_partner_dob is gated on `family_coverage` (coverage_type === "Family").
  const COVERAGE = "sections.benefit_coverage.coverage_type"
  const SPOUSE_DOB = "sections.patient_information.spouse_partner_dob"

  // The gated-off leaf keeps the "N/A" default beginCreate seeded, in an input the
  // user cannot clear — submitting it 422s on the backend's date normalization.
  it("drops a gated-off leaf's seeded default", () => {
    const values = { [COVERAGE]: "Individual", [SPOUSE_DOB]: "N/A" }
    expect(applicableValues(schema, values)[SPOUSE_DOB]).toBe(undefined)
    expect(
      valuesToIntakePayload(applicableValues(schema, values)),
    ).not.toHaveProperty("patient_information.spouse_partner_dob")
  })

  it("keeps the leaf once its gate holds", () => {
    const values = { [COVERAGE]: "Family", [SPOUSE_DOB]: "5/1/1988" }
    expect(applicableValues(schema, values)[SPOUSE_DOB]).toBe("5/1/1988")
  })
})
