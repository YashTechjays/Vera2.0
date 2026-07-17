// Typed wrappers over the verification-call endpoints; each rides `apiRequest` (bearer token,
// envelope unwrap, throws `ApiError`). Calls are created by the queue dispatcher — no manual start-call endpoint.

import { apiRequest } from "@/lib/api/client"

/** A verification call as returned by the control-plane (list rows + publish result). */
export type CallSummary = {
  id: string
  tenant_id: string
  /** current_status — e.g. "initiated" | "active" | "critical" | … (backend enum). */
  status: string
  room_name: string
  patient_name: string | null
  /** ISO-8601; null until the callee answers. */
  started_at: string | null
  created_at: string
  /** One-way, tenant-wide visibility. Once true it never returns to false. */
  published: boolean
  /** True when the current caller owns the call (its initiated_by). */
  is_owner: boolean
}

/** LiveKit join details for a call room. */
export type JoinTokenResponse = {
  token: string
  url: string
  room_name: string
}

/** GET /calls — active calls the caller owns or that are published, newest first. */
export function listCalls(): Promise<CallSummary[]> {
  return apiRequest<CallSummary[]>("/calls")
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

/** On-demand supervisor-handoff summary of a call's transcript so far. */
export type LiveCallSummary = {
  /** "pending" while the call is too young to summarize (fewer than 2 speech turns). */
  status: "ready" | "pending"
  /** The handoff briefing; null while status is "pending". */
  summary: string | null
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
