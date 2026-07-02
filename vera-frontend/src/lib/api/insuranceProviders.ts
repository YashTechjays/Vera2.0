// Platform (super admin) insurance-provider catalog endpoints.
// Mirrors backend api/v1/insurance_providers.py.
import { apiRequest } from "@/lib/api/client"

export type ProviderSummary = {
  id: string
  name: string
  /** "HH:MM:SS" or null. */
  working_hour_start: string | null
  working_hour_end: string | null
  status: string
  created_at: string
}

export type CreateProviderPayload = {
  name: string
  working_hour_start?: string | null
  working_hour_end?: string | null
  status?: string
}

export function listProviders() {
  return apiRequest<ProviderSummary[]>("/insurance-providers")
}

export function createProvider(payload: CreateProviderPayload) {
  return apiRequest<ProviderSummary>("/insurance-providers", { method: "POST", body: payload })
}
