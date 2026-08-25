import { render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { demoSchema as schema } from "@/lib/ibv/mock"
import { leafByPath } from "@/lib/ibv/schema"
import type { FormValues } from "@/lib/ibv/types"
import type { FieldProvenance } from "@/lib/patient-forms/types"

const PATH = vi.hoisted(() => "sections.male_partner_coverage.male_partner_covered")
// Mutable so a test can satisfy the male-partner gates instead of failing them, switch
// the provider mode, inject validation errors, or set the provenance a specific row's
// dispute/marker branches read.
const state = vi.hoisted(() => ({
  values: {} as FormValues,
  provenance: undefined as FieldProvenance | undefined,
  mode: "mock" as string,
  errors: {} as Record<string, string>,
}))

vi.mock("./IbvProvider", async () => {
  const { demoSchema } = await import("@/lib/ibv/mock")
  const { defaultFlags, resolveConfidence } = await import("@/lib/ibv/disputes")
  const dispute = { previousValue: "No", currentValue: "Yes", confidence: 90 }
  return {
    useIbv: () => ({
      schema: demoSchema,
      values: state.values,
      setValue: vi.fn(),
      errors: state.errors,
      mode: state.mode,
      disputeFor: (p: string) => (p === PATH ? dispute : undefined),
      flagsFor: () => defaultFlags(),
      applyDispute: vi.fn(),
      swapDispute: vi.fn(),
      provenanceFor: () => state.provenance,
      confidenceFor: () => resolveConfidence(dispute.confidence, null),
      isPathRequired: () => false,
    }),
  }
})

import { FieldRow } from "./FieldRow"

const GATES_PASS: FormValues = {
  "sections.benefit_coverage.coverage_type": "Family",
  "sections.patient_information.spouse_gender": "Male",
}

function renderRow(values: FormValues, provenance?: FieldProvenance) {
  state.values = values
  state.provenance = provenance
  const leaf = leafByPath(schema).get(PATH)
  if (!leaf) throw new Error(`demo schema lost ${PATH}`)
  render(<FieldRow field={leaf.field} path={PATH} depth={0} gates={leaf.gates} />)
}

beforeEach(() => {
  state.mode = "mock"
  state.errors = {}
})

describe("FieldRow dispute visibility on a gate-failed field (VR2-166)", () => {
  it("still shows the dispute chip and Apply — the dispute blocks completion regardless of gates", () => {
    renderRow({})
    expect(screen.getByTitle("Apply captured value")).toBeInTheDocument()
    expect(screen.getByText("Prior:")).toBeInTheDocument()
  })

  it("disables Swap while the field is inapplicable — it writes a value the reviewer cannot undo", () => {
    renderRow({})
    expect(screen.queryByRole("button", { name: "Swap with prior value" })).toBeNull()
    // Visible but inert, with the reason in the tooltip (VR2-162: hidden read as broken).
    expect(
      screen.getByRole("button", {
        name: "Swap is unavailable while this field is not applicable",
      }),
    ).toBeDisabled()
  })

  it("keeps the input itself disabled while the field is inapplicable", () => {
    renderRow({})
    expect(screen.getByRole("combobox")).toBeDisabled()
  })

  it("offers Swap again once the gates hold", () => {
    renderRow(GATES_PASS)
    expect(screen.getByRole("button", { name: "Swap with prior value" })).toBeInTheDocument()
    expect(screen.getByRole("combobox")).toBeEnabled()
  })
})

describe("FieldRow unverified marker", () => {
  it("shows the Unverified pill when the field's provenance is not authoritative", () => {
    renderRow(GATES_PASS, { attempt: 1, mode: "full", judge: null, authoritative: false })
    expect(screen.getByText("Unverified")).toBeInTheDocument()
  })

  it("hides the pill for an authoritative provenance", () => {
    renderRow(GATES_PASS, { attempt: 1, mode: "full", judge: null, authoritative: true })
    expect(screen.queryByText("Unverified")).toBeNull()
  })
})

describe("FieldRow inline validation message (VR2-206)", () => {
  const MESSAGE = "Enter a valid Tax ID. Please enter a 9-digit Tax ID."

  it("shows the error text under the field in create mode", () => {
    state.mode = "create"
    state.errors = { [PATH]: MESSAGE }
    renderRow(GATES_PASS)
    expect(screen.getByText(MESSAGE)).toBeInTheDocument()
  })

  it("keeps the message tooltip-only outside create mode", () => {
    state.errors = { [PATH]: MESSAGE }
    renderRow(GATES_PASS)
    // The review sheet is dense — the message stays on the hover tooltip there.
    expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument()
  })

  it("renders no message line while the field is valid", () => {
    state.mode = "create"
    renderRow(GATES_PASS)
    expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument()
  })
})
