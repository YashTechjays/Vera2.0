// Platform (super admin) insurance-provider catalog endpoints.
// Mirrors backend api/v1/insurance_providers.py.
import { apiRequest, randomId } from "@/lib/api/client"

/** Mirrors the backend ProviderStatus enum (the DB CHECK-constrains status to these). */
export type ProviderStatus = "active" | "inactive"

export type ProviderSummary = {
  id: string
  name: string
  /** "HH:MM:SS" or null. */
  working_hour_start: string | null
  working_hour_end: string | null
  status: ProviderStatus
  created_at: string
}

export type CreateProviderPayload = {
  name: string
  working_hour_start?: string | null
  working_hour_end?: string | null
  status?: ProviderStatus
}

export type UpdateProviderPayload = {
  name?: string
  working_hour_start?: string | null
  working_hour_end?: string | null
  status?: ProviderStatus
}

export function listProviders() {
  return apiRequest<ProviderSummary[]>("/insurance-providers")
}

export function getProvider(id: string) {
  return apiRequest<ProviderSummary>(`/insurance-providers/${encodeURIComponent(id)}`)
}

export function createProvider(payload: CreateProviderPayload) {
  return apiRequest<ProviderSummary>("/insurance-providers", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": randomId() },
  })
}

export function updateProvider(id: string, patch: UpdateProviderPayload) {
  return apiRequest<ProviderSummary>(`/insurance-providers/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: patch,
    headers: { "Idempotency-Key": randomId() },
  })
}

// Soft delete: the backend deactivates the provider (status → "inactive") rather than
// removing it, so its IVR playbooks survive. Returns the updated (inactive) provider.
export function deleteProvider(id: string) {
  return apiRequest<ProviderSummary>(`/insurance-providers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}
