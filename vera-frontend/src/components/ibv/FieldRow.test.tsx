import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { demoSchema as schema } from "@/lib/ibv/mock"
import { leafByPath } from "@/lib/ibv/schema"

const PATH = vi.hoisted(() => "sections.male_partner_coverage.male_partner_covered")

vi.mock("./IbvProvider", async () => {
  const { demoSchema } = await import("@/lib/ibv/mock")
  const { defaultFlags } = await import("@/lib/ibv/disputes")
  const dispute = { previousValue: "No", currentValue: "Yes", confidence: 90 }
  const ibv = {
    schema: demoSchema,
    // No coverage_type / spouse_gender answered, so the male-partner gates fail.
    values: {},
    setValue: vi.fn(),
    errors: {},
    disputeFor: (p: string) => (p === PATH ? dispute : undefined),
    flagsFor: () => defaultFlags(),
    applyDispute: vi.fn(),
    swapDispute: vi.fn(),
    provenanceFor: () => undefined,
    isPathRequired: () => false,
  }
  return { useIbv: () => ibv }
})

import { FieldRow } from "./FieldRow"

function renderRow() {
  const leaf = leafByPath(schema).get(PATH)
  if (!leaf) throw new Error(`demo schema lost ${PATH}`)
  render(<FieldRow field={leaf.field} path={PATH} depth={0} gates={leaf.gates} />)
}

describe("FieldRow dispute visibility on a gate-failed field (VR2-166)", () => {
  it("still shows the dispute controls — the dispute blocks completion regardless of gates", () => {
    renderRow()
    expect(screen.getByTitle("Swap with prior value")).toBeInTheDocument()
  })

  it("keeps the input itself disabled while the field is inapplicable", () => {
    renderRow()
    expect(screen.getByRole("combobox")).toBeDisabled()
  })
})
