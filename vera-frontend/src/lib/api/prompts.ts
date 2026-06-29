// Platform (super admin) prompt-catalog endpoints. Mirrors backend api/v1/prompts.py.
import { apiRequest } from "@/lib/api/client"

export type CompositeJson = {
  name?: string
  format?: string
  source?: string
  prompt: string
  [key: string]: unknown
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
}

export type PromptVersionDetail = PromptVersionSummary & {
  composite_json: CompositeJson
}

export function listPrompts() {
  return apiRequest<PromptSummary[]>("/prompts")
}

export function listPromptVersions(promptId: string) {
  return apiRequest<PromptVersionSummary[]>(`/prompts/${encodeURIComponent(promptId)}/versions`)
}

export function getPromptVersion(promptId: string, versionId: string) {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}`,
  )
}

export function createPromptDraft(promptId: string, compositeJson: CompositeJson) {
  return apiRequest<PromptVersionDetail>(`/prompts/${encodeURIComponent(promptId)}/versions`, {
    method: "POST",
    body: { composite_json: compositeJson },
  })
}

export function publishPromptVersion(promptId: string, versionId: string) {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}/publish`,
    { method: "POST" },
  )
}
