// Typed wrappers over the outbound integration endpoints. Mirror the backend
// contract (snake_case). The secret credential is write-once: accepted on PUT and
// never returned, so these types only ever carry non-secret metadata.

import { apiRequest } from "@/lib/api/client"

/** A tenant's integration, non-secret view (GET /integrations, PUT response).
 *  `configured` is true once a credential has been sealed; the value is never returned. */
export type Integration = {
  integration_type: string
  status: string
  configured: boolean
  rotated_at: string | null
}

/** GET /integrations — the tenant's integrations (requires integrations:manage). */
export function listIntegrations(): Promise<Integration[]> {
  return apiRequest<Integration[]>("/integrations")
}

/** PUT /integrations/{type} — set/replace the integration's credential. The caller
 *  supplies the credentials object keyed by the type's schema (e.g.
 *  { twilio_sip_trunk: "…" }); the value is envelope-encrypted server-side. */
export function configureIntegration(
  integrationType: string,
  credentials: Record<string, string>,
): Promise<Integration> {
  return apiRequest<Integration>(`/integrations/${encodeURIComponent(integrationType)}`, {
    method: "PUT",
    body: { credentials },
  })
}
