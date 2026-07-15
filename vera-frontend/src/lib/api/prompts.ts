// Platform (super admin) prompt-catalog endpoints. Mirrors backend api/v1/prompts.py:
// composite_json is a PromptDocument (session block + sparse task overrides), never
// compiled text; rendering happens server-side (GET/POST /preview).
import { apiRequest, randomId } from "@/lib/api/client"

export type SessionBlock = {
  persona: string
  goal: string
  base_instructions: string
}

/** Sparse patch over one task's schema-authored text. The server serializes
 *  unset fields as null; treat null and absent identically. */
export type TaskTextOverride = {
  intro?: string | null
  outro?: string | null
  prompt?: string | null
}

export type PromptDocument = {
  kind: "prompt_document"
  session: SessionBlock
  task_overrides: Record<string, TaskTextOverride>
}

export type PromptSummary = {
  id: string
  name: string
  insurance_type: string
  published_version: number | null
}

export type PromptVersionSummary = {
  id: string
  version: number
  status: string
  created_at: string
  /** The schema_version this immutable version pins (renders/validates against). */
  schema_version_id: string
  schema_version: number
}

export type PromptVersionDetail = PromptVersionSummary & {
  composite_json: PromptDocument
}

export type RenderedTaskPrompt = {
  task_key: string
  title: string
  intro: string | null
  outro: string | null
  prompt: string
}

export type RenderedPrompts = {
  name: string
  insurance_type: string
  dsl_version: string
  persona: string
  goal: string
  base_instructions: string
  tasks: RenderedTaskPrompt[]
}

/** The published schema version the next draft will pin (GET /prompts/{id}/schema). */
export type PromptSchemaDetail = {
  id: string
  schema_id: string
  version: number
  status: string
  insurance_type: string
  name: string
  document: unknown
}

/** Stateless dry-run render; `errors` uses the exact save-time 400 strings. */
export type PromptPreview = {
  errors: string[]
  rendered: RenderedPrompts
}

export function listPrompts(): Promise<PromptSummary[]> {
  return apiRequest<PromptSummary[]>("/prompts")
}

export function listPromptVersions(promptId: string): Promise<PromptVersionSummary[]> {
  return apiRequest<PromptVersionSummary[]>(`/prompts/${encodeURIComponent(promptId)}/versions`)
}

export function getPromptVersion(promptId: string, versionId: string): Promise<PromptVersionDetail> {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}`,
  )
}

/** Every save creates a new immutable draft; the body IS the document.
 *  Idempotency-Key follows the write convention (ivrPlaybooks, insuranceProviders);
 *  the backend prompt routes don't enforce it yet (known, deferred). */
export function createPromptDraft(promptId: string, doc: PromptDocument): Promise<PromptVersionDetail> {
  return apiRequest<PromptVersionDetail>(`/prompts/${encodeURIComponent(promptId)}/versions`, {
    method: "POST",
    body: doc,
    headers: { "Idempotency-Key": randomId() },
  })
}

export function publishPromptVersion(promptId: string, versionId: string): Promise<PromptVersionDetail> {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}/publish`,
    { method: "POST" },
  )
}

export function getPromptSchema(promptId: string): Promise<PromptSchemaDetail> {
  return apiRequest<PromptSchemaDetail>(`/prompts/${encodeURIComponent(promptId)}/schema`)
}

/** Authoritative render of a SAVED version (no id → the published one). */
export function previewPromptVersion(promptId: string, versionId?: string): Promise<RenderedPrompts> {
  const base = `/prompts/${encodeURIComponent(promptId)}/preview`
  const path = versionId === undefined ? base : `${base}?version_id=${encodeURIComponent(versionId)}`
  return apiRequest<RenderedPrompts>(path)
}

/** Stateless render of the editing buffer; persists nothing. */
export function previewPromptDocument(promptId: string, doc: PromptDocument): Promise<PromptPreview> {
  return apiRequest<PromptPreview>(`/prompts/${encodeURIComponent(promptId)}/preview`, {
    method: "POST",
    body: doc,
  })
}
