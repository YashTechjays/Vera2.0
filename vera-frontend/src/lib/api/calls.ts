// Typed wrappers over the verification-call endpoints; each rides `apiRequest` (bearer token,
// envelope unwrap, throws `ApiError`). Calls are created by the queue dispatcher — no manual start-call endpoint.

import { apiRequest } from "@/lib/api/client"

/** A verification call as returned by the control-plane (list rows + publish result). */
export type CallSummary = {
  id: string
  tenant_id: string
  /** The patient form this call fills — opens the real form from the monitoring UI. */
  form_id: string
  /** current_status — e.g. "initiated" | "active" | "critical" | … (backend enum). */
  status: string
  room_name: string
  patient_name: string | null
  insurance_provider: string | null
  /** The bound form schema's family — e.g. "infertility_treatment". Not PHI
   *  (a business classification, not patient data). */
  insurance_type: string | null
  /** ISO-8601; null until the callee answers. */
  started_at: string | null
  /** ISO-8601; set once the call reaches a terminal status. */
  ended_at: string | null
  created_at: string
  /** One-way, tenant-wide visibility. Once true it never returns to false. */
  published: boolean
  /** True when the current caller owns the call (its initiated_by). */
  is_owner: boolean
  /** Latest observer health score (0-100); null = never assessed (render neutrally, never 0). */
  health_score: number | null
  /** "none" or an intervention category; null = never assessed. */
  health_flag: string | null
  /** Analyzer's one-line justification (PHI — session-scoped state only);
   *  shown in the health tooltip. Null until first assessed. */
  health_reason: string | null
  /** ISO-8601 time of the latest assessment; drives the staleness gray-out. */
  health_analyzed_at: string | null
  /** Form completion 0-100; null = never projected. Drives the live progress bar's
   *  fallback when no answer has streamed yet this call (e.g. a late retry). */
  completion_pct: number | null
}

/** LiveKit join details for a call room. */
export type JoinTokenResponse = {
  token: string
  url: string
  room_name: string
}

/** Live Monitoring stat-card counts, over the same calls the list shows the caller. */
export type CallStats = {
  /** Calls created today (UTC), any status. */
  total_today: number
  /** In-flight (non-terminal) calls right now. */
  live: number
  critical: number
}

/** GET /calls — calls the caller owns or that are published, newest first.
 *  scope "live" (default) is the in-flight list; "history" the most recent terminal calls. */
export function listCalls(scope: "live" | "history" = "live"): Promise<CallSummary[]> {
  return apiRequest<CallSummary[]>(scope === "history" ? "/calls?scope=history" : "/calls")
}

/** GET /calls/stats — counts for the Live Monitoring stat cards. */
export function getCallStats(): Promise<CallStats> {
  return apiRequest<CallStats>("/calls/stats")
}

/** One row in the tenant-wide Call History list (GET /call-history). Call metadata
 *  plus the patient identifiers the list displays — no field values or transcript. */
export type CallHistoryRow = {
  id: string
  /** The patient form this call fills — links the row to the form detail. */
  form_id: string
  /** "full" (fresh dial) or "retry" (an automatic re-dial). */
  mode: string
  /** current_status — the backend call-status enum (e.g. "completed", "busy"). */
  status: string
  created_at: string
  patient_name: string | null
  member_id: string | null
  insurance_provider: string | null
  /** True only when this caller may actually play the recording (AVAILABLE, visible,
   *  and holds recordings:read) — matches the per-form timeline's gate. */
  recording_available: boolean
}

export type PaginatedCalls = {
  items: CallHistoryRow[]
  page: number
  page_size: number
  total: number
}

export type ListCallHistoryParams = {
  page?: number
  page_size?: number
  /** Filter by call status (backend enum value). */
  status?: string
  /** Case-insensitive search over patient name / member id. */
  q?: string
  /** ISO-8601 lower/upper bounds on the call's created_at. */
  date_from?: string
  date_to?: string
}

/** GET /call-history — the tenant's calls as a flat, newest-first, paginated list
 *  across every form (needs calls:read). The cross-form counterpart to the per-form
 *  call timeline; `recording_available` gates the inline player per row. */
export function listCallHistory(params: ListCallHistoryParams = {}): Promise<PaginatedCalls> {
  const { page = 1, page_size = 20, status, q, date_from, date_to } = params
  const qs = new URLSearchParams({ page: String(page), page_size: String(page_size) })
  if (status) qs.set("status", status)
  if (q) qs.set("q", q)
  if (date_from) qs.set("date_from", date_from)
  if (date_to) qs.set("date_to", date_to)
  return apiRequest<PaginatedCalls>(`/call-history?${qs}`)
}

/** POST /calls/{id}/publish — owner-only, one-way, idempotent. */
export function publishCall(callId: string): Promise<CallSummary> {
  return apiRequest<CallSummary>(`/calls/${encodeURIComponent(callId)}/publish`, {
    method: "POST",
  })
}

/** GET /calls/{id}/join-token — listen-only by default; intervene mints a publish token
 *  (needs calls:intervene; claims the single-intervener lock, 409 while another holds it). */
export function getJoinToken(callId: string, intervene = false): Promise<JoinTokenResponse> {
  const query = intervene ? "?intervene=true" : ""
  return apiRequest<JoinTokenResponse>(`/calls/${encodeURIComponent(callId)}/join-token${query}`)
}

/** POST /calls/{id}/end — tears the room down; the worker's call.ended event drives closeout
 *  (a user-requested end closes as canceled). Allowed for anyone who can watch the call. */
export function endCall(callId: string): Promise<null> {
  return apiRequest<null>(`/calls/${encodeURIComponent(callId)}/end`, { method: "POST" })
}

/** Skimmable sections of the handoff summary (the backend's parsed LLM contract). */
export type LiveCallSummarySections = {
  participants: string | null
  purpose: string | null
  facts: string[]
  open_items: string[]
  next_step: string | null
}

/** On-demand supervisor-handoff summary of a call's transcript so far. */
export type LiveCallSummary = {
  /** "pending" while the call is too young to summarize (fewer than 2 speech turns). */
  status: "ready" | "pending"
  /** The handoff briefing as plain text; null while status is "pending". */
  summary: string | null
  /** Structured view of `summary`; null when the LLM reply didn't parse
   *  (render the plain-text `summary` instead). */
  sections: LiveCallSummarySections | null
  /** Epoch milliseconds the summary was generated (server clock). */
  generated_at: number
  turn_count: number
}

/** GET /calls/{id}/summary — short handoff summary of the transcript so far (needs
 *  calls:read; same visibility as the event stream). The server caches it briefly,
 *  so repeated calls are cheap; 503 SERVICE_UNAVAILABLE when every LLM provider fails. */
export function getCallSummary(callId: string): Promise<LiveCallSummary> {
  return apiRequest<LiveCallSummary>(`/calls/${encodeURIComponent(callId)}/summary`)
}

/** Short-lived signed playback URL for a call's recording (GET /calls/{id}/recording).
 *  Every fetch is audited server-side (RECORDING_ACCESSED) — call it only on an
 *  explicit user action, never to probe availability (the attempt DTO carries that). */
export type RecordingPlayback = { url: string; expires_at: string }

export function getRecordingPlayback(callId: string): Promise<RecordingPlayback> {
  return apiRequest<RecordingPlayback>(`/calls/${encodeURIComponent(callId)}/recording`)
}
