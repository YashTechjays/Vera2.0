import { describe, expect, it } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { allLeaves, createRequiredPaths, parseSchema } from "./schema"
import {
  isoToDateFormat,
  missingCreateLeaves,
  declaredLegalValues,
  normalizePercentValue,
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

describe("normalizePercentValue", () => {
  it("appends the sign to a bare number", () => {
    expect(normalizePercentValue("20")).toBe("20%")
    expect(normalizePercentValue(" 20 ")).toBe("20%")
    expect(normalizePercentValue("12.5")).toBe("12.5%")
  })

  it("is idempotent — blur fires again on tab-through of an untouched cell", () => {
    for (const raw of ["20", "20%", "0", "12.50", "20 percent", "", "20% after deductible"]) {
      const once = normalizePercentValue(raw)
      expect(normalizePercentValue(once)).toBe(once)
    }
  })

  it("gives one spelling per number", () => {
    expect(normalizePercentValue("020")).toBe("20%")
    expect(normalizePercentValue("12.50")).toBe("12.5%")
    expect(normalizePercentValue("20.0")).toBe("20%")
    expect(normalizePercentValue("0")).toBe("0%")
  })

  it("folds the spoken unit suffixes the extractor may emit", () => {
    expect(normalizePercentValue("20 %")).toBe("20%")
    expect(normalizePercentValue("20 percent")).toBe("20%")
    expect(normalizePercentValue("20PCT")).toBe("20%")
  })

  it("passes blank through so a required field can still be cleared", () => {
    // `clearedRequired` in IbvProvider blocks Save on an emptied required field; that
    // depends on "" staying "".
    expect(normalizePercentValue("")).toBe("")
    expect(normalizePercentValue("   ")).toBe("   ")
  })

  it("passes non-numeric values through verbatim", () => {
    expect(normalizePercentValue("N/A")).toBe("N/A")
    expect(normalizePercentValue("None")).toBe("None")
    expect(normalizePercentValue("$20")).toBe("$20")
    expect(normalizePercentValue("20-30%")).toBe("20-30%")
    expect(normalizePercentValue("20% after deductible")).toBe("20% after deductible")
    expect(normalizePercentValue("twenty percent")).toBe("twenty percent")
  })

  it("folds a declared literal to its authored spelling, case-insensitively", () => {
    // Parity with the Python half's `_matched_literal`. Without this, a typed "n/a" stays
    // lowercase, `isDeclaredLegal` compares byte-for-byte against the authored "N/A" and
    // does not short-circuit, and the cell shows a false error (PR #82 review).
    expect(normalizePercentValue("n/a", ["N/A"])).toBe("N/A")
    expect(normalizePercentValue("N/a", ["N/A"])).toBe("N/A")
    expect(normalizePercentValue(" n/a ", ["N/A"])).toBe("N/A")
    expect(normalizePercentValue("none", ["N/A", "None"])).toBe("None")
  })

  it("leaves a typed literal alone when the leaf declares none", () => {
    expect(normalizePercentValue("n/a", [])).toBe("n/a")
  })

  it("folds to a value byte-equal to the real leaf's declared literal", () => {
    // The mechanism behind the regression: `isDeclaredLegal` compares byte-for-byte, so
    // only the folded spelling short-circuits pattern/range for a non-numeric answer.
    const MALE_COINS = "sections.male_partner_coverage.semen_analysis.cpt_89320.coinsurance"
    const leaf = allLeaves(schema).find((l) => l.path === MALE_COINS)!
    const literals = declaredLegalValues(leaf.field)

    expect(literals).toContain("N/A")
    expect(normalizePercentValue("n/a", literals)).toBe(leaf.field.inapplicable_value)
    expect(literals).not.toContain(normalizePercentValue("n/a", []))
  })

  it("does not clamp, so the range check still flags an out-of-range value", () => {
    expect(normalizePercentValue("150")).toBe("150%")
    expect(
      validateAll(schema, { [COVERED]: "Yes", [COINSURANCE]: normalizePercentValue("150") })[
        COINSURANCE
      ],
    ).toBeTruthy()
  })

  it("does not rescale a fraction (the Apps Script 0.2 hazard stays upstream)", () => {
    expect(normalizePercentValue("0.2")).toBe("0.2%")
  })

  it("produces a value the validator accepts", () => {
    const values = { [COVERED]: "Yes", [COINSURANCE]: normalizePercentValue("20") }
    expect(validateAll(schema, values)[COINSURANCE]).toBeUndefined()
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
