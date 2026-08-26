import { render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { demoSchema as schema } from "@/lib/ibv/mock"

const state = vi.hoisted(() => ({ callScopedPaths: new Set<string>() as ReadonlySet<string> }))

vi.mock("./IbvProvider", () => ({ useIbv: () => ({ callScopedPaths: state.callScopedPaths }) }))

import { UsageLegend } from "./UsageLegend"

/**
 * The legend is the only place the colour coding is EXPLAINED, and it hides any usage whose
 * count is zero. So a tint that FieldRow paints but the legend never counts renders as an
 * unexplained colour — which is how the per-call marking first shipped: FieldRow was wired
 * with the call-scoped set and this component was not, so the amber cells appeared with no
 * legend row and the per-call leaves stayed inside "Collected on the call".
 */
const PER_CALL = "sections.insurance_information.plan_type"

/** "Label (12)" → 12, read off the rendered legend rather than recomputed from the schema. */
function countFor(label: string): number {
  const node = screen.queryByText(new RegExp(`^${label} \\(\\d+\\)$`))
  if (!node) return 0
  return Number(/\((\d+)\)/.exec(node.textContent ?? "")?.[1] ?? 0)
}

describe("UsageLegend per-call row", () => {
  beforeEach(() => {
    state.callScopedPaths = new Set()
  })

  it("omits the per-call row when nothing is call-scoped", () => {
    render(<UsageLegend schema={schema} />)
    expect(screen.queryByText(/^Per-call field \(\d+\)$/)).toBeNull()
    expect(countFor("Collected on the call")).toBeGreaterThan(0)
  })

  it("counts a call-scoped path as per-call and takes it OUT of 'Collected on the call'", () => {
    // Baseline first, so the assertion is a shift of exactly one rather than a hardcoded
    // total that drifts whenever the demo schema gains a field.
    const { unmount } = render(<UsageLegend schema={schema} />)
    const askedBefore = countFor("Collected on the call")
    unmount()

    state.callScopedPaths = new Set([PER_CALL])
    render(<UsageLegend schema={schema} />)
    expect(countFor("Per-call field")).toBe(1)
    expect(countFor("Collected on the call")).toBe(askedBefore - 1)
  })

  it("explains WHY the field never disputes — the reason the marking exists", () => {
    state.callScopedPaths = new Set([PER_CALL])
    render(<UsageLegend schema={schema} />)
    const row = screen.getByText(/^Per-call field \(\d+\)$/).closest("p")
    expect(row?.textContent).toMatch(/every call/i)
    expect(row?.textContent).toMatch(/dispute/i)
  })
})
