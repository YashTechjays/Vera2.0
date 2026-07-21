// Presentation helpers for patient-form data — pure string/label mapping, kept
// out of components so they're trivially testable and reused by the worklist +
// review modal.

import type { PatientFormStatus } from "./types"

/** "exception_review" → "Exception Review" */
export function statusLabel(status: string): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ")
}

/** Tailwind chip classes per status (badge background + text). */
export function statusBadgeClass(status: PatientFormStatus | string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-100 text-emerald-700"
    case "exception_review":
      return "bg-amber-100 text-amber-700"
    case "in_queue":
    case "ai_processing":
    case "in_call":
      return "bg-blue-100 text-blue-700"
    case "call_failed":
      return "bg-red-100 text-red-700"
    case "ready_for_processing":
    default:
      return "bg-slate-100 text-slate-600"
  }
}

/** Tailwind chip classes for a call attempt's mode ("full" | "retry"). */
export function modeBadgeClass(mode: "full" | "retry"): string {
  return mode === "retry"
    ? "bg-purple-100 text-purple-700"
    : "bg-slate-100 text-slate-600"
}

// Manual status transitions a reviewer/operator may trigger from the form UI —
// mirrors the backend state machine in patient_forms.py. The call pipeline owns
// every other edge, so only these targets are offered.
const MANUAL_STATUS_TRANSITIONS: Partial<Record<PatientFormStatus, PatientFormStatus[]>> = {
  ready_for_processing: ["in_queue"],
  call_failed: ["in_queue"],
  exception_review: ["in_queue", "completed"],
}

/** Status targets a human may move `status` to (empty for pipeline/terminal states). */
export function allowedStatusTransitions(status: PatientFormStatus): PatientFormStatus[] {
  return MANUAL_STATUS_TRANSITIONS[status] ?? []
}

/** Button label for moving a form to `target`. */
export function statusActionLabel(target: PatientFormStatus): string {
  if (target === "completed") return "Mark complete"
  if (target === "in_queue") return "Send to queue"
  return statusLabel(target)
}

/** Dotted path → its section key (first segment). */
export function sectionOf(fieldPath: string): string {
  return fieldPath.split(".")[0] ?? ""
}

/** "insurance_information" → "Insurance Information" */
export function humanizeSegment(segment: string): string {
  return segment
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ")
}

/** The leaf label for a field row: the path minus its section, humanized.
 *  "insurance_information.health_plan" → "Health Plan";
 *  "general_coverage.office_visits.cpt_1.covered" → "Office Visits › Cpt 1 › Covered" */
export function fieldLabel(fieldPath: string): string {
  const segments = fieldPath.split(".").slice(1)
  return segments.map(humanizeSegment).join(" › ") || humanizeSegment(fieldPath)
}

/** Coerce an API field value (string | number | boolean | null) to an input string. */
export function valueToInput(value: unknown): string {
  if (value === null || value === undefined) return ""
  if (typeof value === "boolean") return value ? "Yes" : "No"
  return String(value)
}

/** ISO timestamp/date → a short locale date, or "—". */
export function formatDate(iso: string | null): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
}

/** Rough relative age for worklist columns: "3d" / "5h" / "12m", or "—". */
export function ageLabel(iso: string | null): string {
  if (!iso) return "—"
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return "—"
  const mins = Math.max(0, Math.floor((Date.now() - t) / 60_000))
  if (mins >= 1440) return `${Math.floor(mins / 1440)}d`
  if (mins >= 60) return `${Math.floor(mins / 60)}h`
  return `${mins}m`
}
