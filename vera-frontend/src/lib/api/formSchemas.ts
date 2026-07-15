// Typed wrappers over the platform form-schema catalog endpoints (read-only).

import { apiRequest } from "@/lib/api/client"

export type FormSchemaSummary = {
  id: string
  name: string
  insurance_type: string
  /** Version number of the single published (active) version, or null. */
  active_version: number | null
  version_count: number
  created_at: string
}

export type SchemaVersionStatus = "draft" | "published"

export type SchemaVersionSummary = {
  id: string
  version: number
  status: SchemaVersionStatus
  published_at: string | null
  created_at: string
}

/** GET /form-schemas — the global schema catalog, ordered by name. */
export function listFormSchemas(): Promise<FormSchemaSummary[]> {
  return apiRequest<FormSchemaSummary[]>("/form-schemas")
}

/** GET /form-schemas/{id}/versions — all versions, newest first. */
export function listSchemaVersions(schemaId: string): Promise<SchemaVersionSummary[]> {
  return apiRequest<SchemaVersionSummary[]>(
    `/form-schemas/${encodeURIComponent(schemaId)}/versions`,
  )
}
