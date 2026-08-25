import { describe, expect, it } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { allLeaves, createRequiredPaths, parseSchema } from "./schema"
import {
  isoToDateFormat,
  missingCreateLeaves,
  validateAll,
  validateCreate,
  validateSection,
} from "./validation"
import type { FormSchema, FormValues } from "./types"

const schema = parseSchema(rawSchema)

const COVERAGE = "sections.benefit_coverage.coverage_type"
const COPAY = "sections.general_coverage.office_visits.cpt_99211.copay"
const COVERED = "sections.general_coverage.office_visits.cpt_99211.covered"
const COINSURANCE = "sections.general_coverage.office_visits.cpt_99211.coinsurance"
const DOB = "sections.patient_information.patient_dob"

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
    ).toMatch(/9-digit/)
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
  it("accepts values matching the schema's date_format (M/D/YYYY)", () => {
    expect(validateAll(schema, { [DOB]: "2/15/1990" })[DOB]).toBeUndefined()
    expect(validateAll(schema, { [DOB]: "02/15/1990" })[DOB]).toBeUndefined()
  })

  it("flags values in another date shape", () => {
    expect(validateAll(schema, { [DOB]: "1990-02-15" })[DOB]).toMatch(/MM\/DD\/YYYY/)
    expect(validateAll(schema, { [DOB]: "Feb 15 1990" })[DOB]).toMatch(/MM\/DD\/YYYY/)
  })

  it("flags shape-valid values that are not real calendar dates", () => {
    expect(validateAll(schema, { [DOB]: "13/15/1990" })[DOB]).toMatch(/MM\/DD\/YYYY/)
    expect(validateAll(schema, { [DOB]: "02/30/1990" })[DOB]).toMatch(/MM\/DD\/YYYY/)
    expect(validateAll(schema, { [DOB]: "0/0/1990" })[DOB]).toMatch(/MM\/DD\/YYYY/)
  })

  it("flags non-calendar dates live in create mode too", () => {
    const errors = validateCreate(schema, { [DOB]: "13/15/1990" }, { includeRequired: false })
    expect(errors[DOB]).toMatch(/MM\/DD\/YYYY/)
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

// Minimal v2 document for validateCreate: one system field without a default (required at create),
// one with a default (exempt), and a voice-required leaf (NOT required at create).
const createSchema = {
  dsl_version: "2.1",
  name: "Test Form",
  sections: {
    patient_information: {
      title: "Patient Information",
      fields: {
        patient_name: { type: "text", title: "Patient Name" },
        patient_gender: {
          type: "enum",
          title: "Gender",
          values: ["Female", "Male"],
          default: "N/A",
        },
        health_plan: {
          type: "text",
          title: "Health Plan",
          required: true,
          validation: { pattern: "^[A-Z].*$" },
        },
      },
    },
  },
  system_fields: {
    patient_name: "sections.patient_information.patient_name",
    patient_gender: "sections.patient_information.patient_gender",
  },
} as unknown as FormSchema

const CREATE_NAME = "sections.patient_information.patient_name"
const CREATE_GENDER = "sections.patient_information.patient_gender"
const CREATE_PLAN = "sections.patient_information.health_plan"

describe("validateCreate", () => {
  it("requires system_fields targets without a default; ignores voice-required leaves", () => {
    const errors = validateCreate(createSchema, {})
    expect(errors[CREATE_NAME]).toBe("Patient Name is required")
    expect(errors[CREATE_GENDER]).toBeUndefined() // default counts as filled
    expect(errors[CREATE_PLAN]).toBeUndefined() // leaf `required` = voice collection, not intake
  })

  it("clears the required error once the system field is filled", () => {
    expect(validateCreate(createSchema, { [CREATE_NAME]: "Jane" })).toEqual({})
  })

  it("still format-checks any filled field", () => {
    const errors = validateCreate(createSchema, { [CREATE_NAME]: "Jane", [CREATE_PLAN]: "bad" })
    expect(errors[CREATE_PLAN]).toBe("Health Plan is invalid")
  })

  it("returns format errors only when includeRequired is false", () => {
    const errors = validateCreate(createSchema, { [CREATE_PLAN]: "bad" }, { includeRequired: false })
    expect(errors[CREATE_PLAN]).toBe("Health Plan is invalid")
    expect(errors[CREATE_NAME]).toBeUndefined()
  })
})

describe("createRequiredPaths", () => {
  it("is system_fields minus defaulted targets, NOT the leaves' own required", () => {
    expect(createRequiredPaths(createSchema)).toEqual(new Set([CREATE_NAME]))
  })

  it("marks a different set than the voice-collection `required` on the real schema", () => {
    const required = createRequiredPaths(schema)
    // The reference sections hold half the create-required fields...
    expect(required).toContain("sections.hospital_information.hospital_address")
    expect(required).toContain("sections.provider_reference_information.npi")
    // ...while a voice-required leaf that is not a system field is NOT create-required.
    const voiceOnly = allLeaves(schema).filter(
      (l) => l.field.required === true && !required.has(l.path),
    )
    expect(voiceOnly.length).toBeGreaterThan(100)
  })
})

const titlesOf = (s: FormSchema, v: FormValues) =>
  missingCreateLeaves(s, v).map((l) => l.field.title)

describe("missingCreateLeaves", () => {
  it("names the blank required fields in document order", () => {
    expect(titlesOf(createSchema, {})).toEqual(["Patient Name"])
    expect(titlesOf(createSchema, { [CREATE_NAME]: "Jane" })).toEqual([])
  })

  it("reports every unfilled required field of the real schema", () => {
    expect(missingCreateLeaves(schema, {})).toHaveLength(createRequiredPaths(schema).size)
  })
})

const DIALED_PHONE = "sections.insurance_reference_information.insurance_phone_number"
const CONTEXT_PHONE = "sections.verification_information.callback_number"

// Dialed-phone E.164 checks: keyed on the promoted handle, not the leaf type, so a
// phone leaf no handle points at stays free-text (backend parity).
const phoneSchema = {
  dsl_version: "2.1",
  name: "Test Form",
  sections: {
    insurance_reference_information: {
      title: "Insurance Reference Information",
      fields: {
        insurance_phone_number: { type: "phone", title: "Insurance Provider Phone" },
      },
    },
    verification_information: {
      title: "Verification Information",
      fields: {
        callback_number: { type: "phone", title: "Callback Number" },
      },
    },
  },
  system_fields: { insurance_provider_phone_number: DIALED_PHONE },
} as unknown as FormSchema

describe("validateAll — dialed phone E.164", () => {
  it("flags a non-E.164 dialed phone value", () => {
    expect(validateAll(phoneSchema, { [DIALED_PHONE]: "555-010-0100" })[DIALED_PHONE]).toBe(
      "Enter a valid Insurance Provider Phone including the country code.",
    )
    expect(validateAll(phoneSchema, { [DIALED_PHONE]: "+1 555 0100" })[DIALED_PHONE]).toMatch(
      /country code/i,
    )
  })

  it("accepts a valid E.164 dialed phone value", () => {
    expect(validateAll(phoneSchema, { [DIALED_PHONE]: "+12125551234" })[DIALED_PHONE]).toBeUndefined()
  })

  it("leaves unbound phone leaves free-text", () => {
    expect(validateAll(phoneSchema, { [CONTEXT_PHONE]: "555-010-0100" })[CONTEXT_PHONE]).toBeUndefined()
  })

  it("applies the same check in create mode", () => {
    const errors = validateCreate(phoneSchema, { [DIALED_PHONE]: "5550100" }, { includeRequired: false })
    expect(errors[DIALED_PHONE]).toMatch(/country code/i)
  })
})

// VR2-206 — specific pre-upload messages, exercised against the real schema.
const APPT_DATE = "sections.appointment_information.appointment_date"
const CALLBACK = "sections.verification_information.callback_number"
const INS_PHONE = "sections.insurance_reference_information.insurance_phone_number"
const TAX_ID = "sections.hospital_information.tax_id"
const FACILITY_NPI = "sections.hospital_information.npi"
const PROVIDER_NPI = "sections.provider_reference_information.npi"

/** Today minus `years` (plus `extraDays`), in the schema's M/D/YYYY shape. */
function dobYearsAgo(years: number, extraDays = 0): string {
  const date = new Date()
  date.setFullYear(date.getFullYear() - years)
  date.setDate(date.getDate() + extraDays)
  return `${date.getMonth() + 1}/${date.getDate()}/${date.getFullYear()}`
}

describe("validateAll — VR2-206 specific messages", () => {
  it("names the digit count for digit-only patterns", () => {
    expect(validateAll(schema, { [TAX_ID]: "12345" })[TAX_ID]).toBe(
      "Enter a valid Tax ID. Please enter a 9-digit Tax ID.",
    )
    expect(validateAll(schema, { [FACILITY_NPI]: "99" })[FACILITY_NPI]).toBe(
      "Enter a valid Facility NPI. Please enter a 10-digit Facility NPI.",
    )
    expect(validateAll(schema, { [PROVIDER_NPI]: "99" })[PROVIDER_NPI]).toBe(
      "Enter a valid Provider NPI. Please enter a 10-digit Provider NPI.",
    )
  })

  it("spells out the date format", () => {
    expect(validateAll(schema, { [DOB]: "1990-02-23" })[DOB]).toBe(
      "Enter Patient Date of Birth in MM/DD/YYYY format.",
    )
    expect(validateAll(schema, { [APPT_DATE]: "23rd Aug" })[APPT_DATE]).toBe(
      "Enter Appointment Date in MM/DD/YYYY format.",
    )
  })

  it("requires the country code on the callback number too", () => {
    expect(validateAll(schema, { [CALLBACK]: "5550199" })[CALLBACK]).toBe(
      "Enter a valid Callback Number including the country code.",
    )
    expect(validateAll(schema, { [CALLBACK]: "+15125550199" })[CALLBACK]).toBeUndefined()
    expect(validateAll(schema, { [INS_PHONE]: "5550100" })[INS_PHONE]).toBe(
      "Enter a valid Insurance Provider Phone including the country code.",
    )
  })

  it("blocks a patient under 18", () => {
    expect(validateAll(schema, { [DOB]: dobYearsAgo(10) })[DOB]).toBe(
      "Patient must be 18 years of age or older.",
    )
    // A day short of the 18th birthday still blocks; 18+ passes.
    expect(validateAll(schema, { [DOB]: dobYearsAgo(18, 1) })[DOB]).toBe(
      "Patient must be 18 years of age or older.",
    )
    expect(validateAll(schema, { [DOB]: dobYearsAgo(18) })[DOB]).toBeUndefined()
    expect(validateAll(schema, { [DOB]: dobYearsAgo(40) })[DOB]).toBeUndefined()
  })

  it("prefers the format message over the age rule and applies both in create mode", () => {
    expect(validateCreate(schema, { [DOB]: "bad" }, { includeRequired: false })[DOB]).toBe(
      "Enter Patient Date of Birth in MM/DD/YYYY format.",
    )
    expect(
      validateCreate(schema, { [DOB]: dobYearsAgo(10) }, { includeRequired: false })[DOB],
    ).toBe("Patient must be 18 years of age or older.")
  })
})

describe("validateAll — numeric consistency (lifetime maximum triplet)", () => {
  const TOTAL = "sections.lifetime_maximum.total"
  const MET = "sections.lifetime_maximum.met_amount"
  const REMAINING = "sections.lifetime_maximum.remaining"

  it("flags the bug-report example on every participating field", () => {
    const errors = validateAll(schema, { [TOTAL]: "$100", [MET]: "$300", [REMAINING]: "$300" })
    for (const path of [TOTAL, MET, REMAINING]) {
      expect(errors[path]).toMatch(/exceeds/i)
    }
    expect(errors[MET]).toContain("$300.00")
    expect(errors[MET]).toContain("$100.00")
  })

  it("flags a sum mismatch when nothing exceeds the total", () => {
    const errors = validateAll(schema, {
      [TOTAL]: "$25,000",
      [MET]: "$5,000",
      [REMAINING]: "$25,000",
    })
    expect(errors[TOTAL]).toMatch(/must equal/i)
  })

  it("accepts a consistent triplet and tolerates one-cent rounding", () => {
    const ok = validateAll(schema, { [TOTAL]: "$25,000", [MET]: "$5,000", [REMAINING]: "$20,000" })
    expect(ok[TOTAL]).toBeUndefined()
    const cent = validateAll(schema, { [TOTAL]: "100", [MET]: "50", [REMAINING]: "50.01" })
    expect(cent[TOTAL]).toBeUndefined()
  })

  it("stays silent on special values and partial data", () => {
    expect(validateAll(schema, { [TOTAL]: "No Limit" })[TOTAL]).toBeUndefined()
    const partial = validateAll(schema, { [TOTAL]: "$100", [MET]: "$50" })
    expect(partial[TOTAL]).toBeUndefined()
    expect(partial[MET]).toBeUndefined()
  })

  // Parity with backend parse_currency: a string that is only currency symbols
  // (no digits) is "absent", not 0 — Number("") is 0, which the backend rejects.
  it("treats symbol-only values as absent, not zero", () => {
    const errors = validateAll(schema, { [TOTAL]: "$", [MET]: "$300", [REMAINING]: "$50" })
    for (const path of [TOTAL, MET, REMAINING]) expect(errors[path]).toBeUndefined()
  })

  // Parity with backend math.isfinite guard: non-finite amounts are "absent".
  it("treats non-finite values as absent", () => {
    const errors = validateAll(schema, { [TOTAL]: "Infinity", [MET]: "$50", [REMAINING]: "$50" })
    for (const path of [TOTAL, MET, REMAINING]) expect(errors[path]).toBeUndefined()
  })
})
