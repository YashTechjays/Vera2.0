import { describe, expect, it } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import {
  allLeaves,
  completionPercent,
  contradictionWarnings,
  fieldUsageOf,
  flattenSection,
  getSectionTable,
  isApplicable,
  isRequired,
  optionsOf,
  parseSchema,
  sectionEntriesOf,
  suggestionsOf,
  systemFieldPaths,
} from "./schema"
import type { FormValues } from "./types"

// The backend's compiled artifact (imported from its source of truth) is the
// test fixture — the same document the backend serves per schema_version_id.
const schema = parseSchema(rawSchema)

const COVERAGE = "sections.benefit_coverage.coverage_type"
const SPOUSE_GENDER = "sections.patient_information.spouse_gender"

function leaf(path: string) {
  const found = allLeaves(schema).find((l) => l.path === path)
  if (!found) throw new Error(`no leaf at ${path}`)
  return found
}

describe("parseSchema", () => {
  it("accepts 2.x documents and rejects anything else", () => {
    expect(parseSchema(rawSchema).dsl_version).toBe("2.1")
    expect(() => parseSchema({ sections: [] })).toThrow(/dsl_version/)
    expect(() => parseSchema({ dsl_version: "1.0" })).toThrow(/dsl_version/)
  })
})

describe("sectionEntriesOf", () => {
  it("returns all 23 sections in document order, including the formerly hidden ones", () => {
    const keys = sectionEntriesOf(schema).map(([k]) => k)
    expect(keys).toHaveLength(23)
    expect(keys[0]).toBe("patient_information")
    expect(keys).toContain("insurance_representative")
    expect(keys).toContain("insurance_reference_information")
  })
})

describe("flattenSection", () => {
  it("produces root-anchored paths", () => {
    const rows = flattenSection("patient_information", schema.sections.patient_information)
    const chart = rows.find((r) => r.key === "chart_number")!
    expect(chart.path).toBe("sections.patient_information.chart_number")
    expect(chart.depth).toBe(0)
  })

  it("emits group rows followed by their children with increased depth", () => {
    const rows = flattenSection("deductibles", schema.sections.deductibles)
    const group = rows.find((r) => r.path === "sections.deductibles.individual")!
    const child = rows.find((r) => r.path === "sections.deductibles.individual.total")!
    expect(group.field.type).toBe("group")
    expect(child.depth).toBe(group.depth + 1)
    expect(rows.indexOf(child)).toBeGreaterThan(rows.indexOf(group))
  })
})

describe("gate chaining (applicable_when)", () => {
  it("family deductible leaves inherit the group's family_coverage gate", () => {
    const l = leaf("sections.deductibles.family.total")
    expect(isApplicable(schema, l.gates, {})).toBe(false)
    expect(isApplicable(schema, l.gates, { [COVERAGE]: "Individual" })).toBe(false)
    expect(isApplicable(schema, l.gates, { [COVERAGE]: "Family" })).toBe(true)
  })

  it("met_amount is skipped when the total is a no-deductible answer", () => {
    const l = leaf("sections.deductibles.individual.met_amount")
    const total = "sections.deductibles.individual.total"
    expect(isApplicable(schema, l.gates, { [total]: "No Limit" })).toBe(false)
    expect(isApplicable(schema, l.gates, { [total]: "$1,500" })).toBe(true)
  })

  it("male_partner_coverage leaves inherit the section gate", () => {
    const l = leaf("sections.male_partner_coverage.male_partner_covered")
    expect(isApplicable(schema, l.gates, { [COVERAGE]: "Family" })).toBe(false)
    expect(
      isApplicable(schema, l.gates, { [COVERAGE]: "Family", [SPOUSE_GENDER]: "Male" })
    ).toBe(true)
  })
})

describe("isRequired", () => {
  it("handles static and conditional requiredness", () => {
    expect(
      isRequired(schema, leaf("sections.patient_information.patient_name").field, {})
    ).toBe(true)
    const spouse = leaf("sections.patient_information.spouse_partner_name").field
    expect(isRequired(schema, spouse, {})).toBe(false)
    expect(isRequired(schema, spouse, { [COVERAGE]: "Family" })).toBe(true)
  })
})

describe("optionsOf / suggestionsOf", () => {
  it("merges special_values into enum options", () => {
    const pa = leaf(
      "sections.general_coverage.office_visits.cpt_99211.prior_auth"
    ).field
    expect(optionsOf(pa)).toEqual(["Yes", "No", "N/A", "Prior auth department"])
  })

  it("exposes text special_values as combobox suggestions", () => {
    const planType = leaf("sections.insurance_information.plan_type").field
    expect(optionsOf(planType)).toEqual([])
    expect(suggestionsOf(planType)).toEqual(["PPO", "HMO", "EPO", "POS"])
  })
})

describe("getSectionTable", () => {
  it("returns null for sections without the ui.layout table hint", () => {
    expect(
      getSectionTable("patient_information", schema.sections.patient_information)
    ).toBeNull()
    expect(getSectionTable("deductibles", schema.sections.deductibles)).toBeNull()
  })

  it("models general_coverage: 3 groups × 1 CPT row, no extra columns", () => {
    const t = getSectionTable("general_coverage", schema.sections.general_coverage)!
    expect(t.columns.map((c) => c.key)).toEqual([
      "covered",
      "copay",
      "coinsurance",
      "prior_auth",
    ])
    expect(t.extraColumns).toEqual([])
    expect(t.hasIcd).toBe(true)
    expect(t.groups.map((g) => g.rows.length)).toEqual([1, 1, 1])
    expect(t.groups[0].rows[0].path).toBe(
      "sections.general_coverage.office_visits.cpt_99211"
    )
    expect(t.groups[0].rows[0].cells.covered?.path).toBe(
      "sections.general_coverage.office_visits.cpt_99211.covered"
    )
  })

  it("models diagnostic_testing: one row per leaf-only CPT group", () => {
    const t = getSectionTable("diagnostic_testing", schema.sections.diagnostic_testing)!
    expect(t.groups).toHaveLength(8)
    expect(t.extraColumns).toEqual([])
    expect(t.groups[0].label).toBe("CPT 58340")
    expect(t.groups[0].rows[0].path).toBe("sections.diagnostic_testing.cpt_58340")
    expect(t.groups[0].rows[0].cells.covered?.path).toBe(
      "sections.diagnostic_testing.cpt_58340.covered"
    )
  })

  it("models male_partner_coverage: section leaf above the table, gated groups", () => {
    const t = getSectionTable(
      "male_partner_coverage",
      schema.sections.male_partner_coverage
    )!
    expect(t.leaves.map((l) => l.key)).toEqual(["male_partner_covered"])
    expect(t.groups.map((g) => g.rows[0].path)).toEqual([
      "sections.male_partner_coverage.semen_analysis.cpt_89320",
      "sections.male_partner_coverage.sperm_cryopreservation.cpt_89259",
    ])
    // group gates include the section gate AND the male_partner_covered gate
    const cell = t.groups[0].rows[0].cells.covered!
    expect(
      isApplicable(schema, cell.gates, {
        [COVERAGE]: "Family",
        [SPOUSE_GENDER]: "Male",
        "sections.male_partner_coverage.male_partner_covered": "Yes",
      })
    ).toBe(true)
    expect(
      isApplicable(schema, cell.gates, {
        [COVERAGE]: "Family",
        [SPOUSE_GENDER]: "Male",
        "sections.male_partner_coverage.male_partner_covered": "No",
      })
    ).toBe(false)
  })

  it("models infertility_treatment: section leaves, nested rows, group extras", () => {
    const t = getSectionTable(
      "infertility_treatment",
      schema.sections.infertility_treatment
    )!
    expect(t.leaves.map((l) => l.key)).toEqual(["infertility_tx_covered"])
    expect(t.extraColumns.map((c) => c.key)).toEqual(["cycle_limit", "additional_notes"])

    const iui = t.groups.find((g) => g.path.endsWith("intrauterine_insemination"))!
    expect(iui.rows).toHaveLength(3)
    expect(iui.rows[0].label).toBe("CPT 58323")
    expect(iui.extras.cycle_limit?.path).toBe(
      "sections.infertility_treatment.intrauterine_insemination.cycle_limit"
    )

    // leaf-only group: the group itself is one row, extras split out by key
    const ovulation = t.groups.find((g) => g.path.endsWith("ovulation_induction"))!
    expect(ovulation.rows).toHaveLength(1)
    expect(ovulation.rows[0].path).toBe(
      "sections.infertility_treatment.ovulation_induction"
    )
    expect(ovulation.rows[0].cells.covered?.path).toBe(
      "sections.infertility_treatment.ovulation_induction.covered"
    )
    expect(ovulation.extras.cycle_limit?.path).toBe(
      "sections.infertility_treatment.ovulation_induction.cycle_limit"
    )
  })

  it("chains section → group → row gates onto every cell", () => {
    const t = getSectionTable(
      "infertility_treatment",
      schema.sections.infertility_treatment
    )!
    const iui = t.groups.find((g) => g.path.endsWith("intrauterine_insemination"))!
    const copay = iui.rows[0].cells.copay!
    const covered = "sections.infertility_treatment.infertility_tx_covered"
    const rowCovered = `${iui.rows[0].path}.covered`
    // copay needs the treatment covered (group gate) AND this CPT covered (own gate)
    expect(
      isApplicable(schema, copay.gates, { [covered]: "Yes", [rowCovered]: "Yes" })
    ).toBe(true)
    expect(
      isApplicable(schema, copay.gates, { [covered]: "No", [rowCovered]: "Yes" })
    ).toBe(false)
    expect(
      isApplicable(schema, copay.gates, { [covered]: "Yes", [rowCovered]: "No" })
    ).toBe(false)
  })
})

describe("completionPercent", () => {
  it("is under 100 for an empty form and reaches 100 when every applicable required leaf is filled", () => {
    expect(completionPercent(schema, {})).toBeLessThan(100)

    // Fill required∧applicable leaves to a fixpoint: answering a gate field can
    // make new leaves applicable, so iterate until stable.
    const values: FormValues = { [COVERAGE]: "Individual" }
    for (let i = 0; i < 10; i++) {
      let changed = false
      for (const l of allLeaves(schema)) {
        if (values[l.path]) continue
        if (!isApplicable(schema, l.gates, values)) continue
        if (!isRequired(schema, l.field, values)) continue
        values[l.path] = l.field.values?.[0] ?? "1"
        changed = true
      }
      if (!changed) break
    }
    expect(completionPercent(schema, values)).toBe(100)
  })

  it("treats a declared default as filled", () => {
    // Some required leaves carry default "N/A" (e.g. patient_gender), so even
    // an empty form is partially complete.
    expect(completionPercent(schema, {})).toBeGreaterThan(0)
  })
})

describe("fieldUsageOf", () => {
  const usage = (path: string) => fieldUsageOf(schema, path, leaf(path).field)

  it("resolves every system_fields binding to a real leaf", () => {
    const paths = systemFieldPaths(schema)
    expect(paths.size).toBeGreaterThan(0)
    const leafPaths = new Set(allLeaves(schema).map((l) => l.path))
    for (const p of paths) expect(leafPaths).toContain(p)
  })

  it("system binding wins over the leaf role", () => {
    // chart_number is role input AND a system field → system
    expect(usage("sections.patient_information.chart_number")).toBe("system")
    expect(usage("sections.patient_information.patient_name")).toBe("system")
  })

  it("classifies bot context, UI-only and asked fields", () => {
    expect(usage("sections.patient_information.spouse_gender")).toBe("context")
    expect(usage("sections.form_information.practice")).toBe("noop")
    expect(usage("sections.insurance_information.plan_type")).toBe("asked")
    // confirm-role fields are collected on the call
    expect(usage("sections.patient_information.spouse_partner_name")).toBe("asked")
  })

  it("treats every leaf of a ui_only SECTION as a no-op, whatever its role", () => {
    const synthetic = parseSchema({
      dsl_version: "2.1",
      name: "T",
      insurance_type: "infertility_treatment",
      sections: {
        s: {
          title: "S",
          role: "ui_only",
          fields: { f: { type: "text", title: "F", role: "context" } },
        },
      },
    })
    expect(
      fieldUsageOf(synthetic, "sections.s.f", synthetic.sections.s.fields.f as never)
    ).toBe("noop")
  })
})

describe("contradictionWarnings", () => {
  it("stays dormant until every referenced field has a value", () => {
    expect(contradictionWarnings(schema, {})).toEqual([])
  })

  it("fires mandate_requires_infertility_coverage", () => {
    const warnings = contradictionWarnings(schema, {
      "sections.benefit_coverage.infertility_plan_mandate": "Yes",
      "sections.infertility_treatment.infertility_tx_covered": "No",
    })
    expect(warnings.map((w) => w.rule_key)).toEqual([
      "mandate_requires_infertility_coverage",
    ])
    expect(warnings[0].reason).toBeTruthy()
  })
})
