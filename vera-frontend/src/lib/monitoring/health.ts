// Pure view logic for the observer health badge (unit-tested; no React).

export type HealthTone = "good" | "warn" | "bad" | "unknown"

/** 3x the observer's analysis interval (15s): older than this means the
 *  observer has gone quiet (LLM outage / silence) — gray out, don't assert. */
const STALE_AFTER_MS = 45_000

export function healthTone(score: number | null): HealthTone {
  if (score === null) return "unknown"
  if (score >= 70) return "good"
  if (score >= 40) return "warn"
  return "bad"
}

/** Never-assessed (null) is "not yet", not "stale". */
export function isHealthStale(analyzedAt: string | null, nowMs: number): boolean {
  if (!analyzedAt) return false
  const t = Date.parse(analyzedAt)
  return Number.isFinite(t) && nowMs - t > STALE_AFTER_MS
}

export function healthDisplay(
  score: number | null,
  analyzedAt: string | null,
  nowMs: number,
): { text: string; tone: HealthTone; stale: boolean } {
  if (score === null) return { text: "Assessing…", tone: "unknown", stale: false }
  return { text: `${score}%`, tone: healthTone(score), stale: isHealthStale(analyzedAt, nowMs) }
}
