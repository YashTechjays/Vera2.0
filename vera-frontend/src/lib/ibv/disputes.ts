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
  /** supporting transcript evidence — the field-level text from the REST detail, or the
   *  frame's own evidence on the live SSE path */
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

/** Mark each disputed path in `paths` applied, preserving any swap (matching the
 *  per-field ✓, which resolves with whatever value the field shows); paths carrying
 *  no dispute are skipped, so callers may pass a section's whole leaf list. */
export function applyFlagsForPaths(
  disputes: DisputeMap,
  flags: DisputeFlagMap,
  paths: Iterable<string>
): DisputeFlagMap {
  const next: DisputeFlagMap = { ...flags }
  for (const path of paths) {
    if (!(path in disputes)) continue
    next[path] = { ...(next[path] ?? defaultFlags()), applied: true }
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

/** Map a confidence score to a level. The bands are 10 points wide from 95 down, so
 *  "high" is a range rather than the single value 100 the smart-caller-fe port used —
 *  a 99% judge verdict no longer reads the same as a 90% one. */
export function confidenceLevel(score?: number): ConfidenceLevel {
  if (score === undefined || score === null) return "unknown"
  if (score >= 95) return "high"
  if (score >= 85) return "medium"
  if (score >= 75) return "low"
  return "very-low"
}

/** Which pass produced the confidence a field displays. */
export type ConfidenceSource = "judge" | "captured"

/**
 * The single confidence a field shows. Two different numbers reach the browser:
 * the extractor's own score, stamped when the answer was captured (live during the
 * call, or by the post-call top-up), and the post-call judge's verdict on whether
 * the transcript supports that value. Showing both bare — in two tooltips on one
 * row — is what made them unreadable, so one wins and says which it is.
 *
 * The judge wins when it has run. That mirrors the backend: `load_field_status`
 * gates retries on the judge's score and falls back to the extractor's, so the
 * number on screen is now the number the system acted on.
 */
export type FieldConfidence = {
  score?: number
  source: ConfidenceSource
  /** false only when the judge explicitly rejected the value */
  supported: boolean
}

export function resolveConfidence(
  captured: number | undefined,
  judge: { confidence: number | null; supported: boolean } | null | undefined
): FieldConfidence {
  if (!judge) return { score: captured, source: "captured", supported: true }
  return { score: judge.confidence ?? undefined, source: "judge", supported: judge.supported }
}

/** A judge-rejected value is always "very-low" whatever its score: the judge prompt
 *  never says whether that number grades the value or the rejection, so trusting it
 *  here would paint a rejected field green. */
export function fieldConfidenceLevel(c: FieldConfidence): ConfidenceLevel {
  return c.supported ? confidenceLevel(c.score) : "very-low"
}

/** Chip text — one number, named by the pass that produced it. An unsupported
 *  verdict shows no number, for the reason in `fieldConfidenceLevel`. */
export function confidenceLabel(c: FieldConfidence): string {
  if (!c.supported) return "judge · unsupported"
  return `${c.source} ${c.score ?? "—"}% · ${fieldConfidenceLevel(c)}`
}

/**
 * Tailwind border+bg+ring classes for an unresolved disputed field, by
 * confidence. smart-caller-fe palette on the confidenceLevel bands: ≥95 green,
 * 85–94 yellow, 75–84 amber, <75 red; unknown → base navy. 1px border + 2px ring.
 */
// Inset ring, never `border` or an outer glow: both draw outside the grid line and
// leave a visible gap between the label column and the highlighted cell (VR2-162).
export function confidenceHighlightClass(level: ConfidenceLevel): string {
  switch (level) {
    case "high":
      return "bg-[#F0FDF4] shadow-[inset_0_0_0_1px_#10b981]"
    case "medium":
      return "bg-[#FEFCE8] shadow-[inset_0_0_0_1px_#eab308]"
    case "low":
      return "bg-[#FFFBEB] shadow-[inset_0_0_0_1px_#f59e0b]"
    case "very-low":
      return "bg-[#FEF2F2] shadow-[inset_0_0_0_1px_#ef4444]"
    default:
      return "bg-[#EFF6FF] shadow-[inset_0_0_0_1px_#003e64]"
  }
}

/** Tailwind classes for the small confidence chip in the tooltip. */
export function confidenceChipClass(level: ConfidenceLevel): string {
  switch (level) {
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
 *  - ≥95 → green (high), 85–94 → yellow (medium), 75–84 → amber (low),
 *    <75 → red (very-low), no confidence → navy (base).
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
    confidence: 88,
    evidence: "Rep confirmed the plan type during the call.",
    reasoning: "Plan type corrected from the carrier portal during the call.",
  },
  "sections.general_coverage.office_visits.cpt_99211.covered": {
    previousValue: "No",
    currentValue: "Yes",
    confidence: 78,
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
