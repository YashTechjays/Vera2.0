import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { demoSchema as schema } from "@/lib/ibv/mock"
import { leafByPath } from "@/lib/ibv/schema"
import type { FormValues } from "@/lib/ibv/types"

const PATH = vi.hoisted(() => "sections.male_partner_coverage.male_partner_covered")
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
      disputeFor: (p: string) => (p === PATH ? dispute : undefined),
      flagsFor: () => defaultFlags(),
      applyDispute: vi.fn(),
      swapDispute: vi.fn(),
      provenanceFor: () => undefined,
      isPathRequired: () => false,
    }),
  }
})

import { FieldRow } from "./FieldRow"

const GATES_PASS: FormValues = {
  "sections.benefit_coverage.coverage_type": "Family",
  "sections.patient_information.spouse_gender": "Male",
}

function renderRow(values: FormValues) {
  state.values = values
  const leaf = leafByPath(schema).get(PATH)
  if (!leaf) throw new Error(`demo schema lost ${PATH}`)
  render(<FieldRow field={leaf.field} path={PATH} depth={0} gates={leaf.gates} />)
}

describe("FieldRow dispute visibility on a gate-failed field (VR2-166)", () => {
  it("still shows the dispute chip and Apply — the dispute blocks completion regardless of gates", () => {
    renderRow({})
    expect(screen.getByTitle("Apply captured value")).toBeInTheDocument()
    expect(screen.getByText("Prior:")).toBeInTheDocument()
  })

  it("hides Swap while the field is inapplicable — it writes a value the reviewer cannot undo", () => {
    renderRow({})
    expect(screen.queryByTitle("Swap with prior value")).toBeNull()
  })

  it("keeps the input itself disabled while the field is inapplicable", () => {
    renderRow({})
    expect(screen.getByRole("combobox")).toBeDisabled()
  })

  it("offers Swap again once the gates hold", () => {
    renderRow(GATES_PASS)
    expect(screen.getByTitle("Swap with prior value")).toBeInTheDocument()
    expect(screen.getByRole("combobox")).toBeEnabled()
  })
})
