// Inline dispute model (smart-caller-fe parity): a field where the assistant
// captured a value (currentValue) that disagrees with the prior value
// (previousValue). The captured value is pre-seeded into the field; per-field
// flags track the apply (✓ / ↶) and swap (⇄) interactions. Pure helpers
// here are unit-tested in disputes.test.ts.

import type { FormValues } from "./types"

export type Dispute = {
  /** the prior / original value (shown in the badge by default) */
  previousValue: string
  /** the assistant-captured value (pre-seeded into the field) */
  currentValue: string
  /** 0–100 confidence the AI answer carried for the captured value */
  confidence?: number
  /** short supporting evidence */
  evidence?: string
  /** model reasoning */
  reasoning?: string
}

/** Disputes keyed by dotted field path. */
export type DisputeMap = Record<string, Dispute>

/** Per-field interaction flags. */
export type DisputeFlags = {
  /** the dispute has been confirmed/applied (resolved) */
  applied: boolean
  /** the input shows the prior value instead of the captured value */
  swapped: boolean
}

export type DisputeFlagMap = Record<string, DisputeFlags>

export function defaultFlags(): DisputeFlags {
  return { applied: false, swapped: false }
}

/** The value currently "primary" for the field (what Apply would commit). */
export function activeDisputeValue(d: Dispute, f: DisputeFlags): string {
  return f.swapped ? d.previousValue : d.currentValue
}

/** The value shown in the badge (the non-active alternative). */
export function badgeValue(d: Dispute, f: DisputeFlags): string {
  return f.swapped ? d.currentValue : d.previousValue
}

export function toggleApplied(f: DisputeFlags): DisputeFlags {
  return { ...f, applied: !f.applied }
}
export function toggleSwapped(f: DisputeFlags): DisputeFlags {
  return { ...f, swapped: !f.swapped }
}

/** Mark each disputed path in `paths` applied (non-swapped); paths carrying no
 *  dispute are skipped, so callers may pass a section's whole leaf list. */
export function applyFlagsForPaths(
  disputes: DisputeMap,
  flags: DisputeFlagMap,
  paths: Iterable<string>
): DisputeFlagMap {
  const next: DisputeFlagMap = { ...flags }
  for (const path of paths) {
    if (!(path in disputes)) continue
    next[path] = { ...(next[path] ?? defaultFlags()), applied: true, swapped: false }
  }
  return next
}

export type SavePayload = {
  formData: FormValues
  /** paths whose dispute was applied/resolved */
  disputeFields: string[]
}

/** Build the per-person save payload from current values + dispute flags. */
export function buildSavePayload(
  values: FormValues,
  disputes: DisputeMap,
  flags: DisputeFlagMap
): SavePayload {
  const disputeFields: string[] = []
  for (const path of Object.keys(disputes)) {
    const f = flags[path] ?? defaultFlags()
    if (f.applied) disputeFields.push(path)
  }
  return { formData: { ...values }, disputeFields }
}

/** Pre-seed field values with each dispute's captured (current) value. */
export function seedValues(disputes: DisputeMap): FormValues {
  const out: FormValues = {}
  for (const [path, d] of Object.entries(disputes)) out[path] = d.currentValue
  return out
}

/** "sections.insurance_information.plan_type" -> "Insurance Information › Plan Type" */
export function humanizeLabel(path: string): string {
  return path
    .replace(/^sections\./, "")
    .split(".")
    .filter(Boolean)
    .map((seg) => seg.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()))
    .join(" › ")
}

export type ConfidenceLevel = "high" | "medium" | "low" | "very-low" | "unknown"

/** Map a confidence score to a level (matches smart-caller-fe thresholds). */
export function confidenceLevel(score?: number): ConfidenceLevel {
  if (score === undefined || score === null) return "unknown"
  if (score >= 100) return "high"
  if (score >= 90) return "medium"
  if (score >= 80) return "low"
  return "very-low"
}

/**
 * Tailwind border+bg+ring classes for an unresolved disputed field, by
 * confidence. Exact smart-caller-fe palette: 100% green, 90–99% yellow,
 * 80–89% amber, <80% red; unknown → base navy. Full 1px border + 2px ring.
 */
export function confidenceHighlightClass(score?: number): string {
  switch (confidenceLevel(score)) {
    case "high":
      return "border border-[#10b981] bg-[#F0FDF4] shadow-[0_0_0_2px_rgba(16,185,129,0.2)]"
    case "medium":
      return "border border-[#eab308] bg-[#FEFCE8] shadow-[0_0_0_2px_rgba(234,179,8,0.2)]"
    case "low":
      return "border border-[#f59e0b] bg-[#FFFBEB] shadow-[0_0_0_2px_rgba(245,158,11,0.2)]"
    case "very-low":
      return "border border-[#ef4444] bg-[#FEF2F2] shadow-[0_0_0_2px_rgba(239,68,68,0.2)]"
    default:
      return "border border-[#003e64] bg-[#EFF6FF] shadow-[0_0_0_2px_rgba(25,88,247,0.2)]"
  }
}

/** Tailwind classes for the small confidence chip in the tooltip. */
export function confidenceChipClass(score?: number): string {
  switch (confidenceLevel(score)) {
    case "high":
      return "bg-[#10b981] text-white"
    case "medium":
      return "bg-[#eab308] text-white"
    case "low":
      return "bg-[#f59e0b] text-white"
    case "very-low":
      return "bg-[#ef4444] text-white"
    default:
      return "bg-muted text-muted-foreground"
  }
}

/**
 * Demo disputes spanning every confidence color. currentValue matches the mock
 * values so the seeded display stays consistent.
 *  - 100 → green (high), 90–99 → yellow (medium), 80–89 → amber (low),
 *    <80 → red (very-low), no confidence → navy (base).
 */
export const mockDisputes: DisputeMap = {
  "sections.patient_information.patient_name": {
    previousValue: "Ava D.",
    currentValue: "Ava Davis",
    confidence: 100,
    evidence: "Full legal name confirmed against the member record.",
    reasoning: "Captured the complete name from the eligibility record.",
  },
  "sections.insurance_information.plan_type": {
    previousValue: "POS",
    currentValue: "PPO",
    confidence: 95,
    evidence: "Rep confirmed the plan type during the call.",
    reasoning: "Plan type corrected from the carrier portal during the call.",
  },
  "sections.general_coverage.office_visits.cpt_99211.covered": {
    previousValue: "No",
    currentValue: "Yes",
    confidence: 92,
    evidence: "Confirmed covered for CPT 99211.",
    reasoning: "Office visit is a covered benefit.",
  },
  "sections.insurance_information.policy_number": {
    previousValue: "POL-550410",
    currentValue: "POL-550411",
    confidence: 85,
    evidence: "Rep read the number back.",
    reasoning: "Final digit corrected when the rep read it back.",
  },
  "sections.benefit_coverage.coverage_type": {
    previousValue: "Individual",
    currentValue: "Family",
    confidence: 72,
    evidence: "Rep confirmed dependents on the plan.",
    reasoning: "Rep confirmed the plan is family coverage.",
  },
  "sections.insurance_information.group_name": {
    previousValue: "Umbrella Hlth",
    currentValue: "Umbrella Health",
    evidence: "Spelled out by the representative.",
    reasoning: "Expanded the abbreviated group name.",
  },
}
