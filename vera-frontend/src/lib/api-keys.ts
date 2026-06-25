// Typed wrappers over the inbound API-key endpoints. Mirror the backend contract
// (snake_case). The list endpoint returns metadata only — the plaintext token is
// returned just once, by create.

import { apiRequest, randomId } from "@/lib/api/client"

/** One inbound API key's metadata (GET /api-keys). No token is ever returned here. */
export type ApiKey = {
  id: string
  name: string
  scope: string
  expires_at: string | null
  revoked: boolean
}

/** A selectable capability for the create form (GET /api-keys/scopes). */
export type ApiKeyScope = {
  code: string
  description: string
}

/** The created key — the ONLY time `token` is returned (POST /api-keys). */
export type CreatedApiKey = {
  id: string
  name: string
  scope: string
  expires_at: string | null
  token: string
}

/** GET /api-keys — list the tenant's inbound API keys (requires apikeys:manage). */
export function listApiKeys(): Promise<ApiKey[]> {
  return apiRequest<ApiKey[]>("/api-keys")
}

/** GET /api-keys/scopes — the scope vocabulary for the create dropdown. */
export function listApiKeyScopes(): Promise<ApiKeyScope[]> {
  return apiRequest<ApiKeyScope[]>("/api-keys/scopes")
}

/** POST /api-keys — issue a new key; the response carries the one-time token. */
export function createApiKey(name: string, scope: string): Promise<CreatedApiKey> {
  return apiRequest<CreatedApiKey>("/api-keys", {
    method: "POST",
    body: { name, scope },
    // Mutating ingress requires an idempotency key (one per attempt).
    headers: { "Idempotency-Key": randomId() },
  })
}

/** POST /api-keys/{id}/revoke — revoke a key (frees its name for reuse). */
export function revokeApiKey(id: string): Promise<null> {
  return apiRequest<null>(`/api-keys/${encodeURIComponent(id)}/revoke`, { method: "POST" })
}
