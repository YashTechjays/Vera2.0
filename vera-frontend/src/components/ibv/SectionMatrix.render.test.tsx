import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { demoSchema as schema } from "@/lib/ibv/mock"
import { getSectionTable } from "@/lib/ibv/schema"
import type { FormValues } from "@/lib/ibv/types"

const CELL = vi.hoisted(
  () => "sections.male_partner_coverage.semen_analysis.cpt_89320.covered",
)
// Mutable so a test can satisfy the male-partner gates instead of failing them.
const state = vi.hoisted(() => ({ values: {} as FormValues }))

vi.mock("./IbvProvider", async () => {
  const { demoSchema } = await import("@/lib/ibv/mock")
  const { defaultFlags } = await import("@/lib/ibv/disputes")
  const dispute = { previousValue: "No", currentValue: "Yes", confidence: 90 }
  return {
    useIbv: () => ({
      schema: demoSchema,
      values: state.values,
      setValue: vi.fn(),
      commitValue: vi.fn(),
      errors: {},
      disputes: { [CELL]: dispute },
      disputeFor: (p: string) => (p === CELL ? dispute : undefined),
      flagsFor: () => defaultFlags(),
      applyDispute: vi.fn(),
      swapDispute: vi.fn(),
    }),
  }
})

import { SectionMatrix } from "./SectionMatrix"

const malePartner = getSectionTable(
  "male_partner_coverage",
  schema.sections.male_partner_coverage
)!

const GATES_PASS: FormValues = {
  "sections.benefit_coverage.coverage_type": "Family",
  "sections.patient_information.spouse_gender": "Male",
  "sections.male_partner_coverage.male_partner_covered": "Yes",
}

function renderMatrix(values: FormValues) {
  state.values = values
  render(<SectionMatrix table={malePartner} />)
}

describe("SectionMatrix dispute controls on a gate-failed cell (VR2-166)", () => {
  it("keeps Apply so the dispute can still be resolved", () => {
    renderMatrix({})
    expect(screen.getAllByTitle("Apply captured value").length).toBeGreaterThan(0)
  })

  it("hides Swap — it would write a value into a cell the reviewer cannot edit", () => {
    renderMatrix({})
    expect(screen.queryByTitle("Swap with prior value")).toBeNull()
  })

  it("offers Swap again once the gates hold", () => {
    renderMatrix(GATES_PASS)
    expect(screen.getAllByTitle("Swap with prior value").length).toBeGreaterThan(0)
  })
})
