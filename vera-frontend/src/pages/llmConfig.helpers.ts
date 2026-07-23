import type { LlmConfigState, ThinkingOverride } from "@/lib/api/llmConfig"

export const SUGGESTED_MODELS = [
  "gemini-2.5-flash",
  "gemini-3.1-flash-lite",
  "gemini-3.5-flash",
  "gemini-3.6-flash",
] as const

export const THINKING_LEVELS = ["minimal", "low", "medium", "high"] as const

/** Mirrors the backend's vera_core.services.model_config.is_gemini_3_model exactly —
 *  keep both in lockstep; a drifted heuristic would let the wrong thinking control
 *  render for a model name the backend would reject. */
export function isGemini3Model(model: string): boolean {
  return model.toLowerCase().includes("gemini-3")
}

/** Whether either the model input or the thinking override differs from what's
 *  currently saved — gates the Save button so a no-op save isn't offered. A
 *  default (model: null) reads as "". */
export function hasPendingChange(
  input: string,
  extraConfig: ThinkingOverride | null,
  current: LlmConfigState,
): boolean {
  if (input.trim() !== (current.model ?? "")) return true
  return JSON.stringify(extraConfig) !== JSON.stringify(current.extra_config)
}

/** Reset only makes sense when an override is actually active. */
export function canReset(current: LlmConfigState): boolean {
  return !current.is_default
}

export function formatUpdatedAt(iso: string | null): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
}
