import type { ProviderOption } from "./types"

/** The catalog provider id whose name matches a form's insurance_provider string
 *  (case-insensitive, trimmed), or "" when none matches. Mirrors the backend's
 *  dispatch-time resolution so the send-to-queue picker pre-selects exactly what
 *  dispatch would resolve. */
export function matchProvider(
  providers: ProviderOption[],
  name: string | null,
): string {
  if (!name) return ""
  const target = name.trim().toLowerCase()
  return providers.find((p) => p.name.trim().toLowerCase() === target)?.id ?? ""
}
