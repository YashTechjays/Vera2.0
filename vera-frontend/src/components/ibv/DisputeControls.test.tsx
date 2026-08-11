import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"

import { DisputeTooltipBody, InlineDisputeControls } from "./DisputeControls"
import { defaultFlags, resolveConfidence, type Dispute } from "@/lib/ibv/disputes"

// DisputeTooltipBody is rendered directly rather than through a hover: Radix
// portals its content through floating-ui, which does not settle under jsdom.
// The body is exported precisely so its contract can be asserted in isolation.
function dispute(extra: Partial<Dispute> = {}): Dispute {
  return {
    previousValue: "BCBS TX",
    currentValue: "Blue Cross",
    confidence: 90,
    evidence: "Agent: which plan? Rep: Blue Cross.",
    ...extra,
  }
}

describe("DisputeTooltipBody", () => {
  it("shows the confidence, both values and the evidence", () => {
    render(
      <DisputeTooltipBody
        dispute={dispute()}
        confidence={resolveConfidence(90, null)}
      />
    )
    expect(screen.getByText(/captured 90% · medium/)).toBeInTheDocument()
    expect(screen.getByText("Prior:").parentElement).toHaveTextContent("BCBS TX")
    expect(screen.getByText("Captured:").parentElement).toHaveTextContent("Blue Cross")
    expect(screen.getByText("Evidence:").parentElement).toHaveTextContent(
      "Agent: which plan? Rep: Blue Cross."
    )
  })

  it("renders no attempt/judge provenance line", () => {
    // A reviewer resolving a dispute acts on the values and the quote; which attempt
    // produced them is noise here. The judge only reaches the chip, via resolveConfidence.
    render(
      <DisputeTooltipBody
        dispute={dispute()}
        confidence={resolveConfidence(90, { confidence: 88, supported: true })}
      />
    )
    expect(screen.queryByText(/Attempt/)).not.toBeInTheDocument()
    expect(screen.queryByText(/supported/)).not.toBeInTheDocument()
    expect(screen.getByText(/judge 88% · low/)).toBeInTheDocument()
  })

  it("shows the evidence for an answer whose judge rejected it", () => {
    // The rejected chip carries no number, but the quote is exactly what the reviewer
    // needs to adjudicate — it must survive the unsupported path.
    render(
      <DisputeTooltipBody
        dispute={dispute()}
        confidence={resolveConfidence(95, { confidence: 95, supported: false })}
      />
    )
    expect(screen.getByText("judge · unsupported")).toBeInTheDocument()
    expect(screen.getByText("Evidence:").parentElement).toHaveTextContent(
      "Agent: which plan? Rep: Blue Cross."
    )
  })
})

describe("InlineDisputeControls", () => {
  it("exposes the dispute chip as a focusable button", () => {
    // Regression guard: the chip is the only keyboard path to the tooltip in the
    // wide layout now that the label-cell (i) button is gone. A <span> trigger
    // would silently make the evidence mouse-only again.
    render(
      <InlineDisputeControls
        dispute={dispute()}
        confidence={resolveConfidence(90, null)}
        flags={defaultFlags()}
        className=""
        canSwap
        onSwap={vi.fn()}
        onApply={vi.fn()}
      />
    )
    expect(screen.getByRole("button", { name: /dispute details/i })).toBeInTheDocument()
  })
})
