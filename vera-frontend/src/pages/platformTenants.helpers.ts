// Pure helpers for the platform Tenants screen — the slug rules and the edit diff,
// testable without rendering.

import type { TenantDetail, UpdateTenantInput } from "@/lib/api/platform"

/** Mirrors the backend's DNS-label rule: lowercase alphanumerics and inner hyphens, 1–63 chars. */
const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/

export const SLUG_MAX_LENGTH = 63

export function isValidSlug(slug: string): boolean {
  return SLUG_PATTERN.test(slug)
}

/** Suggest a slug from an organisation name, or "" when nothing usable remains — which the
 *  form rejects on submit rather than sending a bad value. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, SLUG_MAX_LENGTH)
    .replace(/-+$/, "")
}

/** Fields the platform edit form may change. `slug` is immutable and `status` moves
 *  only through deactivate/reactivate, so neither is ever diffed. */
const EDITABLE_FIELDS = [
  "name",
  "region",
  "observer_enabled",
  "auto_retry_enabled",
  "retry_fill_threshold",
  "max_agents_per_va",
  "max_concurrent_calls",
  "max_retries",
  "queue_expiry_hours",
  "recording_retention_days",
] as const satisfies readonly (keyof UpdateTenantInput)[]

type EditableField = (typeof EDITABLE_FIELDS)[number]

/** What the edit form holds. `slug`/`status` are accepted but ignored, so passing a whole
 *  tenant row can never smuggle an immutable field into the PATCH. */
export type TenantFormValues = Pick<TenantDetail, EditableField> &
  Partial<Pick<TenantDetail, "slug" | "status">>

/** Only the fields that actually differ, so a PATCH never rewrites untouched settings
 *  (and an empty result means "nothing to save"). */
export function changedTenantFields(
  original: TenantDetail,
  form: TenantFormValues,
): UpdateTenantInput {
  const patch: Record<string, unknown> = {}
  for (const field of EDITABLE_FIELDS) {
    if (form[field] !== original[field]) patch[field] = form[field]
  }
  return patch as UpdateTenantInput
}
