// Typed wrapper over the Voice Lab session endpoint. Rides `apiRequest`, which
// injects the bearer token, unwraps the response envelope, and throws `ApiError`
// on failure (e.g. a 409 when outbound SIP is not configured).

import { apiRequest } from "@/lib/api/client"

export type VoiceSessionMode = "browser" | "outbound"

export type StartVoiceSessionPayload = {
  mode: VoiceSessionMode
  /** Required + E.164 when mode === "outbound". */
  phone_number?: string
  /** ON → the worker boots the generic IVR navigator agent instead of the chat persona. */
  enable_ivr_navigation?: boolean
  /** When navigating, the provider whose active playbook specializes the navigator (else generic). */
  insurance_provider_id?: string
  /** Opaque patient-form id — the call plan is compiled from that form's pinned schema.
   *  Unset → the generic published schema for the insurance type. */
  form_id?: string
}

export type VoiceSessionResponse = {
  room_name: string
  url: string
  token: string
  mode: VoiceSessionMode
}

/** An insurance provider the operator can pick when starting an IVR call. */
export type ProviderOption = {
  id: string
  name: string
}

/** GET /voice-lab/insurance-providers — active providers, readable by any operator with
 *  calls:read (tenant users included), for the call-start provider picker. */
export function listCallProviders(): Promise<ProviderOption[]> {
  return apiRequest<ProviderOption[]>("/voice-lab/insurance-providers")
}

/** POST /voice-lab/sessions — create an ephemeral room + browser join token. */
export function startVoiceSession(
  payload: StartVoiceSessionPayload,
): Promise<VoiceSessionResponse> {
  return apiRequest<VoiceSessionResponse>("/voice-lab/sessions", {
    method: "POST",
    body: payload,
  })
}

/** DELETE /voice-lab/sessions/{room_name} — tear the room down server-side so the
 *  agent worker and any outbound SIP call actually end (not just the browser leaving). */
export function endVoiceSession(roomName: string): Promise<null> {
  return apiRequest<null>(`/voice-lab/sessions/${encodeURIComponent(roomName)}`, {
    method: "DELETE",
  })
}
