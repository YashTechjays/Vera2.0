import { describe, expect, it } from "vitest"

import { hasDispute } from "./SectionMatrix"
import { defaultFlags, type DisputeFlagMap, type DisputeMap } from "@/lib/ibv/disputes"
import { demoSchema as schema } from "@/lib/ibv/mock"
import { getSectionTable } from "@/lib/ibv/schema"
import type { FormValues } from "@/lib/ibv/types"

const COVERAGE = "sections.benefit_coverage.coverage_type"
const SPOUSE_GENDER = "sections.patient_information.spouse_gender"

const general = getSectionTable("general_coverage", schema.sections.general_coverage)!
const GENERAL_CELL = "sections.general_coverage.office_visits.cpt_99211.covered"

const malePartner = getSectionTable(
  "male_partner_coverage",
  schema.sections.male_partner_coverage
)!
const MALE_CELL = "sections.male_partner_coverage.semen_analysis.cpt_89320.covered"

const disputeOn = (path: string): DisputeMap => ({
  [path]: { previousValue: "No", currentValue: "Yes" },
})

function context(over: {
  disputes?: DisputeMap
  flags?: DisputeFlagMap
  values?: FormValues
}) {
  const flags = over.flags ?? {}
  return {
    disputes: over.disputes ?? {},
    flagsFor: (path: string) => flags[path] ?? defaultFlags(),
    schema,
    values: over.values ?? {},
  }
}

describe("hasDispute (wide-width gating)", () => {
  it("is false for a table with no disputes", () => {
    expect(hasDispute(general, context({}))).toBe(false)
  })

  it("is true while a cell's dispute is unapplied", () => {
    expect(hasDispute(general, context({ disputes: disputeOn(GENERAL_CELL) }))).toBe(true)
  })

  it("is false again once every dispute on the table is applied", () => {
    const ctx = context({
      disputes: disputeOn(GENERAL_CELL),
      flags: { [GENERAL_CELL]: { applied: true, swapped: false } },
    })
    expect(hasDispute(general, ctx)).toBe(false)
  })

  it("ignores disputes on cells whose gates make them inapplicable", () => {
    const disputes = disputeOn(MALE_CELL)
    expect(hasDispute(malePartner, context({ disputes }))).toBe(false)
    const values = {
      [COVERAGE]: "Family",
      [SPOUSE_GENDER]: "Male",
      "sections.male_partner_coverage.male_partner_covered": "Yes",
    }
    expect(hasDispute(malePartner, context({ disputes, values }))).toBe(true)
  })
})
