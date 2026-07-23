// Platform (super admin) voice-cascade LLM model override endpoints.
// Mirrors backend api/v1/llm_config.py.
import { apiRequest, randomId } from "@/lib/api/client"

export type ThinkingOverride =
  | { thinking_budget: number; thinking_level?: never }
  | { thinking_level: "minimal" | "low" | "medium" | "high"; thinking_budget?: never }

export type LlmConfigState = {
  provider: string | null
  model: string | null
  extra_config: ThinkingOverride | null
  is_default: boolean
  created_at: string | null
  created_by_user_id: string | null
}

export function getLlmConfig() {
  return apiRequest<LlmConfigState>("/platform/llm-config")
}

export function getLlmConfigHistory() {
  return apiRequest<LlmConfigState[]>("/platform/llm-config/history")
}

export function saveLlmConfig(model: string, extraConfig: ThinkingOverride | null = null) {
  return apiRequest<LlmConfigState>("/platform/llm-config", {
    method: "PUT",
    body: { model, extra_config: extraConfig },
    headers: { "Idempotency-Key": randomId() },
  })
}

export function resetLlmConfig() {
  return apiRequest<LlmConfigState>("/platform/llm-config/reset", {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
  })
}
