// Plain-text export of the handoff summary, for the copy-summary button.

import type { LiveCallSummary, LiveCallSummarySections } from "@/lib/api/calls"

function sectionsText(sections: LiveCallSummarySections): string {
  const parts: string[] = []
  if (sections.participants) parts.push(`Participants: ${sections.participants}`)
  if (sections.purpose) parts.push(`Purpose: ${sections.purpose}`)
  if (sections.facts.length > 0)
    parts.push(["Established so far:", ...sections.facts.map((fact) => `- ${fact}`)].join("\n"))
  if (sections.open_items.length > 0)
    parts.push(["Open items:", ...sections.open_items.map((item) => `- ${item}`)].join("\n"))
  if (sections.next_step) parts.push(`Next step: ${sections.next_step}`)
  return parts.join("\n\n")
}

/** The summary as copyable text — sectioned when parsed, else the plain LLM text. */
export function summaryText(result: LiveCallSummary | null): string {
  if (!result || result.status !== "ready") return ""
  if (result.sections) return sectionsText(result.sections)
  return result.summary ?? ""
}
