import { isTerminalCallStatus } from "@/lib/api/callEvents"
import type { CallSummary } from "@/lib/api/calls"
import { elapsed } from "@/lib/monitoring/liveTimer"
import type { CallCategory, LiveCall } from "@/lib/mock-data"

export function categoryOf(status: string): CallCategory {
  const s = status.toLowerCase()
  if (s === "critical") return "critical"
  if (isTerminalCallStatus(s)) return "completed"
  if (s === "waiting" || s === "ivr") return "processing"
  return "active"
}

/** Adapt a real call into the modal's LiveCall shape; confidence is still a placeholder
 *  (the API doesn't provide it yet). */
export function toLiveCall(c: CallSummary, now: number): LiveCall {
  return {
    id: c.id,
    patient: c.patient_name || "—",
    type: "Patient",
    agent: "—",
    duration: elapsed(c.started_at, now),
    status: c.status,
    category: categoryOf(c.status),
    visible: c.published,
    action: c.is_owner ? "view" : "intervene",
    insurance: c.insurance_provider || "—",
    confidence: 0,
    formProgress: c.completion_pct ?? 0,
    verifiedProgress: c.verified_pct ?? 0,
    formId: c.form_id,
    callTime: elapsed(c.started_at, now),
    startedAt: c.started_at,
    healthScore: c.health_score,
    isOwner: c.is_owner,
  }
}
