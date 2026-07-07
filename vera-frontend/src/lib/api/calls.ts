// Typed wrappers over the verification-call endpoints, mirroring `voiceLab.ts`.
// Each rides `apiRequest`, which injects the bearer token, unwraps the response
// envelope, and throws `ApiError` on failure.

import { apiRequest } from "@/lib/api/client"

/** A verification call as returned by the control-plane (list rows + publish result). */
export type CallSummary = {
  id: string
  tenant_id: string
  form_id: string
  /** current_status — e.g. "initiated" | "active" | "critical" | … (backend enum). */
  status: string
  room_name: string
  patient_name: string | null
  /** ISO-8601; null until the call actually starts. */
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

/** POST /calls — start a call for a patient form; the caller becomes the owner. */
export function startCall(formId: string): Promise<CallSummary> {
  return apiRequest<CallSummary>("/calls", { method: "POST", body: { form_id: formId } })
}

/** POST /calls/{id}/publish — owner-only, one-way, idempotent. */
export function publishCall(callId: string): Promise<CallSummary> {
  return apiRequest<CallSummary>(`/calls/${encodeURIComponent(callId)}/publish`, {
    method: "POST",
  })
}

/** GET /calls/{id}/join-token — listen-only by default; intervene mints a
 *  token that may publish audio (non-owners only for a published call). */
export function getJoinToken(callId: string, intervene = false): Promise<JoinTokenResponse> {
  const query = intervene ? "?intervene=true" : ""
  return apiRequest<JoinTokenResponse>(`/calls/${encodeURIComponent(callId)}/join-token${query}`)
}

/** POST /calls/{id}/revoke-access — owner ejects an intervener from the room. */
export function revokeAccess(callId: string, targetUserId: string): Promise<null> {
  return apiRequest<null>(`/calls/${encodeURIComponent(callId)}/revoke-access`, {
    method: "POST",
    body: { target_user_id: targetUserId },
  })
}
