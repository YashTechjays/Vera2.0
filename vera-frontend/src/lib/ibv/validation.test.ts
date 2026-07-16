import { describe, expect, it } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { parseSchema } from "./schema"
import { isoToDateFormat, validateAll, validateSection } from "./validation"

const schema = parseSchema(rawSchema)

const COVERAGE = "sections.benefit_coverage.coverage_type"
const COPAY = "sections.general_coverage.office_visits.cpt_99211.copay"
const COVERED = "sections.general_coverage.office_visits.cpt_99211.covered"
const COINSURANCE = "sections.general_coverage.office_visits.cpt_99211.coinsurance"

describe("validateAll — requiredness", () => {
  it("flags an empty required field without a default", () => {
    expect(validateAll(schema, {})["sections.patient_information.patient_name"]).toMatch(
      /required/i
    )
  })

  it("clears the error once the field is filled", () => {
    const errors = validateAll(schema, {
      "sections.patient_information.patient_name": "Sarah Johnson",
    })
    expect(errors["sections.patient_information.patient_name"]).toBeUndefined()
  })

  it("does not flag a required field with a declared default", () => {
    // appointment_type is required but carries default "N/A"
    expect(
      validateAll(schema, {})["sections.appointment_information.appointment_type"]
    ).toBeUndefined()
  })

  it("applies conditional requiredness (spouse name only for Family coverage)", () => {
    const family = validateAll(schema, { [COVERAGE]: "Family" })
    expect(family["sections.patient_information.spouse_partner_name"]).toBeUndefined()
    // spouse_partner_name has default "N/A" so it's never missing; spouse fields
    // without defaults would flag — use family deductible instead:
    expect(family["sections.deductibles.family.total"]).toMatch(/required/i)
  })

  it("never flags an inapplicable field, even a required one", () => {
    const individual = validateAll(schema, { [COVERAGE]: "Individual" })
    expect(individual["sections.deductibles.family.total"]).toBeUndefined()
    expect(validateAll(schema, {})["sections.deductibles.family.total"]).toBeUndefined()
  })
})

describe("validateAll — pattern and range", () => {
  it("checks validation.pattern on text fields", () => {
    expect(
      validateAll(schema, { "sections.hospital_information.tax_id": "12345" })[
        "sections.hospital_information.tax_id"
      ]
    ).toMatch(/invalid/i)
    expect(
      validateAll(schema, { "sections.hospital_information.tax_id": "123456789" })[
        "sections.hospital_information.tax_id"
      ]
    ).toBeUndefined()
  })

  it("checks validation.range on currency/percent, tolerating $ , % formatting", () => {
    const base = { [COVERED]: "Yes" }
    expect(validateAll(schema, { ...base, [COPAY]: "$1,500.50" })[COPAY]).toBeUndefined()
    expect(validateAll(schema, { ...base, [COPAY]: "-5" })[COPAY]).toMatch(/between|at least/i)
    expect(validateAll(schema, { ...base, [COINSURANCE]: "150%" })[COINSURANCE]).toMatch(
      /between|at most/i
    )
    expect(validateAll(schema, { ...base, [COINSURANCE]: "20%" })[COINSURANCE]).toBeUndefined()
  })

  it("accepts declared special values verbatim on ranged fields", () => {
    const base = { [COVERED]: "Yes" }
    expect(validateAll(schema, { ...base, [COPAY]: "$0" })[COPAY]).toBeUndefined()
    expect(validateAll(schema, { ...base, [COPAY]: "None" })[COPAY]).toBeUndefined()
  })

  it("flags a non-numeric answer on a ranged field", () => {
    const base = { [COVERED]: "Yes" }
    expect(validateAll(schema, { ...base, [COPAY]: "call back later" })[COPAY]).toMatch(/number/i)
  })
})

describe("validateAll — date format", () => {
  const DOB = "sections.patient_information.patient_dob"

  it("accepts values matching the schema's date_format (M/D/YYYY)", () => {
    expect(validateAll(schema, { [DOB]: "2/15/1990" })[DOB]).toBeUndefined()
    expect(validateAll(schema, { [DOB]: "02/15/1990" })[DOB]).toBeUndefined()
  })

  it("flags values in another date shape", () => {
    expect(validateAll(schema, { [DOB]: "1990-02-15" })[DOB]).toMatch(/M\/D\/YYYY/)
    expect(validateAll(schema, { [DOB]: "Feb 15 1990" })[DOB]).toMatch(/M\/D\/YYYY/)
  })
})

describe("isoToDateFormat", () => {
  it("reformats ISO into M/D/YYYY without zero padding", () => {
    expect(isoToDateFormat("1982-02-23", "M/D/YYYY")).toBe("2/23/1982")
    expect(isoToDateFormat("2026-11-05", "M/D/YYYY")).toBe("11/5/2026")
  })

  it("honors padded and two-digit-year token variants", () => {
    expect(isoToDateFormat("1982-02-23", "MM/DD/YYYY")).toBe("02/23/1982")
    expect(isoToDateFormat("1982-02-23", "D.M.YY")).toBe("23.2.82")
  })

  it("passes through anything that is not a bare ISO date", () => {
    expect(isoToDateFormat("2/23/1982", "M/D/YYYY")).toBe("2/23/1982")
    expect(isoToDateFormat("N/A", "M/D/YYYY")).toBe("N/A")
    expect(isoToDateFormat("", "M/D/YYYY")).toBe("")
    expect(isoToDateFormat("1982-02-23T00:00:00", "M/D/YYYY")).toBe("1982-02-23T00:00:00")
  })
})

describe("validateSection", () => {
  it("returns only errors for the given section", () => {
    const errors = validateSection(schema, "insurance_information", {})
    expect(Object.keys(errors).length).toBeGreaterThan(0)
    expect(
      Object.keys(errors).every((p) => p.startsWith("sections.insurance_information."))
    ).toBe(true)
  })
})
