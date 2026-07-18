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
