import type { LlmConfigState } from "@/lib/api/llmConfig"

export const SUGGESTED_MODELS = [
  "gemini-2.5-flash",
  "gemini-3.1-flash-lite",
  "gemini-3.5-flash",
  "gemini-3.6-flash",
] as const

/** Whether the input differs from the currently saved effective value — gates the
 *  Save button so a no-op save isn't offered. A default (model: null) reads as "". */
export function hasPendingChange(input: string, current: LlmConfigState): boolean {
  return input.trim() !== (current.model ?? "")
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
