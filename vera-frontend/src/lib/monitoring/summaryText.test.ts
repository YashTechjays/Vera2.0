import { describe, expect, it } from "vitest"

import type { LiveCallSummary } from "@/lib/api/calls"
import { summaryText } from "./summaryText"

function makeSummary(overrides: Partial<LiveCallSummary>): LiveCallSummary {
  return {
    status: "ready",
    summary: "Plain fallback text.",
    sections: null,
    generated_at: 1755500000000,
    turn_count: 12,
    ...overrides,
  }
}

describe("summaryText", () => {
  it("renders every section with its label, lists as bullets", () => {
    const text = summaryText(
      makeSummary({
        sections: {
          participants: "Vera and a UHC rep",
          purpose: "Verify infertility coverage",
          facts: ["Policy is active", "Deductible met"],
          open_items: ["CPT 58322 coverage"],
          next_step: "Ask about prior authorization",
        },
      }),
    )
    expect(text).toBe(
      [
        "Participants: Vera and a UHC rep",
        "Purpose: Verify infertility coverage",
        "Established so far:\n- Policy is active\n- Deductible met",
        "Open items:\n- CPT 58322 coverage",
        "Next step: Ask about prior authorization",
      ].join("\n\n"),
    )
  })

  it("skips empty sections", () => {
    const text = summaryText(
      makeSummary({
        sections: {
          participants: null,
          purpose: "Verify coverage",
          facts: [],
          open_items: [],
          next_step: null,
        },
      }),
    )
    expect(text).toBe("Purpose: Verify coverage")
  })

  it("falls back to the plain-text summary when sections did not parse", () => {
    expect(summaryText(makeSummary({ sections: null }))).toBe("Plain fallback text.")
  })

  it("is empty while pending or absent", () => {
    expect(summaryText(makeSummary({ status: "pending", summary: null }))).toBe("")
    expect(summaryText(null)).toBe("")
  })
})
