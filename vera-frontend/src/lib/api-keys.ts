// Typed wrapper over the inbound API-key endpoints. Mirrors the backend contract
// (snake_case). The list endpoint returns metadata only — never the token, which
// the backend shows just once at creation.

import { apiRequest } from "@/lib/api/client"

/** One inbound API key's metadata (GET /api-keys). No token is ever returned. */
export type ApiKey = {
  id: string
  name: string
  scope: string
  expires_at: string | null
  revoked: boolean
}

/** GET /api-keys — list the tenant's inbound API keys (requires apikeys:manage). */
export function listApiKeys(): Promise<ApiKey[]> {
  return apiRequest<ApiKey[]>("/api-keys")
}
