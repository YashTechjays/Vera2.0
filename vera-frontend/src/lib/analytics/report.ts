export type PresetKey = "7d" | "30d" | "90d" | "week" | "month" | "custom"

const DAY_MS = 86_400_000

/** UTC ranges so the buckets line up with the backend's UTC day convention. */
export function presetRange(
  preset: Exclude<PresetKey, "custom">,
  now: Date,
): { date_from: string; date_to: string } {
  const date_to = now.toISOString()
  switch (preset) {
    case "7d":
      return { date_from: new Date(now.getTime() - 7 * DAY_MS).toISOString(), date_to }
    case "30d":
      return { date_from: new Date(now.getTime() - 30 * DAY_MS).toISOString(), date_to }
    case "90d":
      return { date_from: new Date(now.getTime() - 90 * DAY_MS).toISOString(), date_to }
    case "week": {
      const mondayOffset = (now.getUTCDay() + 6) % 7
      const monday = Date.UTC(
        now.getUTCFullYear(),
        now.getUTCMonth(),
        now.getUTCDate() - mondayOffset,
      )
      return { date_from: new Date(monday).toISOString(), date_to }
    }
    case "month": {
      const first = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)
      return { date_from: new Date(first).toISOString(), date_to }
    }
  }
}

export function deltaPct(current: number | null, previous: number | null): number | null {
  if (current === null || previous === null || previous === 0) return null
  return ((current - previous) / previous) * 100
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—"
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

export function formatPct(value: number | null, opts?: { fraction?: boolean }): string {
  if (value === null) return "—"
  const pct = opts?.fraction ? value * 100 : value
  return `${pct.toFixed(1)}%`
}
