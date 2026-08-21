import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"

import { DisputeTooltipBody, InlineDisputeControls } from "./DisputeControls"
import { defaultFlags, resolveConfidence, type Dispute } from "@/lib/ibv/disputes"
import { modeBadgeClass } from "@/lib/patient-forms/display"

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
        provenance={null}
      />
    )
    expect(screen.getByText(/captured 90% · medium/)).toBeInTheDocument()
    expect(screen.getByText("Prior:").parentElement).toHaveTextContent("BCBS TX")
    expect(screen.getByText("Captured:").parentElement).toHaveTextContent("Blue Cross")
    expect(screen.getByText("Evidence:").parentElement).toHaveTextContent(
      "Agent: which plan? Rep: Blue Cross."
    )
  })

  it("names no attempt, and no separate judge line, without provenance", () => {
    // The judge reaches the chip and nowhere else (via resolveConfidence) — the duplicate
    // verdict line is what made this tooltip unreadable. Attempt appears only when the
    // field HAS provenance; see the attempt-attribution block below.
    render(
      <DisputeTooltipBody
        dispute={dispute()}
        confidence={resolveConfidence(90, { confidence: 88, supported: true })}
        provenance={null}
      />
    )
    expect(screen.queryByText(/Attempt/)).not.toBeInTheDocument()
    expect(screen.queryByText(/supported/)).not.toBeInTheDocument()
    expect(screen.getByText(/judge 88% · medium/)).toBeInTheDocument()
  })

  it("shows the evidence for an answer whose judge rejected it", () => {
    // The rejected chip carries no number, but the quote is exactly what the reviewer
    // needs to adjudicate — it must survive the unsupported path.
    render(
      <DisputeTooltipBody
        dispute={dispute()}
        confidence={resolveConfidence(95, { confidence: 95, supported: false })}
        provenance={null}
      />
    )
    expect(screen.getByText("judge · unsupported")).toBeInTheDocument()
    expect(screen.getByText("Evidence:").parentElement).toHaveTextContent(
      "Agent: which plan? Rep: Blue Cross."
    )
  })
})

// The two disputed leaves a reviewer meets on the Infertility Treatment grid: a
// row-level `covered` cell and the group-level `cycle_limit` rowspan extra. Real
// schema paths (data/form_schemas/ibv_form_standard_v2.json) so a schema rename
// breaks this test rather than silently making it fictional.
const IVF = "sections.infertility_treatment.in_vitro_fertilization"
const COVERED_PATH = `${IVF}.cpt_58970.covered`
const CYCLE_LIMIT_PATH = `${IVF}.cycle_limit`

const COVERED_DISPUTE: Dispute = {
  previousValue: "No",
  currentValue: "Yes",
  evidence: "Agent: is IVF covered under CPT 58970? Rep: yes, it is covered.",
  reasoning: "Rep confirmed IVF is a covered benefit for this plan.",
}
const CYCLE_LIMIT_DISPUTE: Dispute = {
  previousValue: "2",
  currentValue: "3",
  evidence: "Agent: how many IVF cycles? Rep: three per lifetime.",
  reasoning: "Rep stated a three-cycle lifetime limit.",
}

/** Chip background per band — confidenceChipClass, asserted so a band that maps to
 *  the wrong colour fails even when the label reads correctly. */
const BAND_BG = {
  high: "bg-[#10b981]",
  medium: "bg-[#eab308]",
  low: "bg-[#f59e0b]",
  "very-low": "bg-[#ef4444]",
} as const

// Judge score -> chip, at every band floor and the point just below it.
const JUDGE_BANDS: ReadonlyArray<[number, keyof typeof BAND_BG, string]> = [
  [100, "high", "judge 100% · high"],
  [95, "high", "judge 95% · high"],
  [94, "medium", "judge 94% · medium"],
  [85, "medium", "judge 85% · medium"],
  [84, "low", "judge 84% · low"],
  [75, "low", "judge 75% · low"],
  [74, "very-low", "judge 74% · very-low"],
  [0, "very-low", "judge 0% · very-low"],
]

describe.each([
  ["covered", COVERED_PATH, COVERED_DISPUTE],
  ["cycle_limit", CYCLE_LIMIT_PATH, CYCLE_LIMIT_DISPUTE],
])("judge confidence bands on Infertility Treatment %s", (_field, path, d) => {
  it("anchors on a real schema path", () => {
    expect(path).toMatch(/^sections\.infertility_treatment(\.[a-z0-9_]+)+$/)
  })

  it.each(JUDGE_BANDS)("a supported judge score of %i reads %s", (score, band, label) => {
    render(
      <DisputeTooltipBody
        dispute={d}
        confidence={resolveConfidence(60, { confidence: score, supported: true })}
        provenance={null}
      />
    )
    const chip = screen.getByText(label)
    expect(chip).toBeInTheDocument()
    expect(chip.className).toContain(BAND_BG[band])
    // The judge's number won: the extractor's 60 must not reach the chip.
    expect(screen.queryByText(/captured/)).not.toBeInTheDocument()
  })

  it("shows no number at all when the judge rejected the value", () => {
    render(
      <DisputeTooltipBody
        dispute={d}
        confidence={resolveConfidence(60, { confidence: 91, supported: false })}
        provenance={null}
      />
    )
    const chip = screen.getByText("judge · unsupported")
    // A rejected value is red whatever its score — 91 would otherwise read green.
    expect(chip.className).toContain(BAND_BG["very-low"])
    expect(screen.queryByText(/91/)).not.toBeInTheDocument()
  })

  it("falls back to the extractor's score when the judge never ran", () => {
    render(
      <DisputeTooltipBody dispute={d} confidence={resolveConfidence(92, null)} provenance={null} />
    )
    const chip = screen.getByText("captured 92% · medium")
    expect(chip.className).toContain(BAND_BG.medium)
  })

  it("reads unknown when neither pass produced a score", () => {
    render(
      <DisputeTooltipBody
        dispute={d}
        confidence={resolveConfidence(undefined, null)}
        provenance={null}
      />
    )
    expect(screen.getByText("captured —% · unknown")).toBeInTheDocument()
  })

  it("keeps the reviewer's evidence and both values beside the chip", () => {
    render(
      <DisputeTooltipBody
        dispute={d}
        confidence={resolveConfidence(60, { confidence: 88, supported: true })}
        provenance={null}
      />
    )
    expect(screen.getByText("Prior:").parentElement).toHaveTextContent(d.previousValue)
    expect(screen.getByText("Captured:").parentElement).toHaveTextContent(d.currentValue)
    expect(screen.getByText("Evidence:").parentElement).toHaveTextContent(d.evidence!)
  })
})

describe("attempt attribution in the tooltip", () => {
  // The label-cell (i) also told the reviewer WHICH call attempt captured the value.
  // Removing it took that with it; Call History only answers the inverse (attempt →
  // fields), so the per-field direction lives here now.
  it("names the attempt and mode that captured the value", () => {
    render(
      <DisputeTooltipBody
        dispute={dispute()}
        confidence={resolveConfidence(90, null)}
        provenance={{ attempt: 2, mode: "retry", judge: null, authoritative: true }}
      />
    )
    expect(screen.getByText("Attempt 2")).toBeInTheDocument()
    // The same mode presentation Call History uses, so "retry" reads the same in both.
    expect(screen.getByText("retry").className).toContain(modeBadgeClass("retry"))
  })
})

describe("InlineDisputeControls", () => {
  it("exposes the chip as a button whose name carries the value", () => {
    // Two guards on one element. The chip must be a <button>: it is the only keyboard
    // path to the tooltip now that the label-cell (i) is gone, and a <span> trigger
    // would silently make the evidence mouse-only. And its aria-label REPLACES that
    // text, so the value has to be in the label or a screen reader never hears it.
    render(
      <InlineDisputeControls
        dispute={dispute()}
        confidence={resolveConfidence(90, null)}
        provenance={null}
        flags={defaultFlags()}
        className=""
        canSwap
        onSwap={vi.fn()}
        onApply={vi.fn()}
      />
    )
    expect(screen.getByRole("button", { name: /dispute details/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Prior: BCBS TX/ })).toBeInTheDocument()
  })
})
