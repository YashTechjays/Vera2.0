// Typed wrappers over the tenant concurrency-config endpoints (gated by
// tenant:config:manage server-side). Mirrors the backend contract (snake_case).

import { apiRequest } from "@/lib/api/client"

/** Both knobs: per-VA in-flight cap and the tenant-wide dial ceiling. */
export type ConcurrencyConfig = {
  max_agents_per_va: number
  max_concurrent_calls: number
}

export function getConcurrencyConfig(): Promise<ConcurrencyConfig> {
  return apiRequest<ConcurrencyConfig>("/tenant/config/concurrency")
}

/** PATCH semantics: omitted knobs stay unchanged. No Idempotency-Key — the
 * backend route has no idempotency dependency. */
export function patchConcurrencyConfig(
  patch: Partial<ConcurrencyConfig>,
): Promise<ConcurrencyConfig> {
  return apiRequest<ConcurrencyConfig>("/tenant/config/concurrency", {
    method: "PATCH",
    body: patch,
  })
}
