// Platform (super admin) per-provider IVR playbook endpoints.
// Mirrors backend api/v1/ivr_playbooks.py. A playbook is a non-PHI navigation overlay
// (config knobs that specialize the generic IVR navigator) attached to an insurance provider.
import { apiRequest, randomId } from "@/lib/api/client"

/** The structured config overlay stored in ivr_playbook.instructions. All fields optional;
 *  an unset field falls back to the generic navigator's built-in default. */
export type IvrPlaybookInstructions = {
  transition_trigger?: string | null
  rep_keyword?: string | null
  multiple_patients_answer?: string | null
  survey_answer?: string | null
  date_scope?: string | null
  callback_vs_hold?: string | null
  provider_subflows?: string | null
  extra_rules?: string | null
}

export type PlaybookSummary = {
  id: string
  provider_id: string
  status: string
  created_at: string
}

export type PlaybookDetail = PlaybookSummary & {
  instructions: IvrPlaybookInstructions
  updated_at: string
}

export type CreatePlaybookPayload = {
  provider_id: string
  instructions: IvrPlaybookInstructions
  status?: string
}

export type UpdatePlaybookPayload = {
  instructions?: IvrPlaybookInstructions
  status?: string
}

export function listPlaybooks(providerId?: string) {
  const query = providerId ? `?provider_id=${encodeURIComponent(providerId)}` : ""
  return apiRequest<PlaybookSummary[]>(`/ivr-playbooks${query}`)
}

export function getPlaybook(id: string) {
  return apiRequest<PlaybookDetail>(`/ivr-playbooks/${encodeURIComponent(id)}`)
}

export function createPlaybook(payload: CreatePlaybookPayload) {
  return apiRequest<PlaybookDetail>("/ivr-playbooks", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": randomId() },
  })
}

export function updatePlaybook(id: string, patch: UpdatePlaybookPayload) {
  return apiRequest<PlaybookDetail>(`/ivr-playbooks/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: patch,
  })
}

// DELETE intentionally returns no body: serializing the row would re-validate its payload,
// and delete must still work on a row whose stored instructions no longer validate.
export function deletePlaybook(id: string) {
  return apiRequest<null>(`/ivr-playbooks/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}
