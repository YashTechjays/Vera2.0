import type { InterventionDayRow } from "@/lib/api/analytics"

export type PresetKey = "7d" | "30d" | "90d" | "week" | "month" | "custom"

export type DateRange = { date_from: string; date_to: string }

const DAY_MS = 86_400_000

/** UTC ranges so the buckets line up with the backend's UTC day convention. */
export function presetRange(preset: Exclude<PresetKey, "custom">, now: Date): DateRange {
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

const ZERO_INTERVENTIONS = { flag: 0, coach: 0, whisper: 0, takeover: 0 }

/** Union of both series' days, sorted and zero-filled, so the report charts share one axis. */
export function mergeInterventionDays(
  callsPerDay: { day: string }[],
  interventionsPerDay: InterventionDayRow[],
): InterventionDayRow[] {
  const byDay = new Map(interventionsPerDay.map((row) => [row.day, row]))
  const days = new Set([...callsPerDay.map((c) => c.day), ...byDay.keys()])
  return [...days].sort().map((day) => byDay.get(day) ?? { day, ...ZERO_INTERVENTIONS })
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

/** "2026-08-05" → "Aug 5", for axis ticks; the tooltip keeps the full ISO day. */
export function formatDay(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  })
}

export function formatPct(value: number | null, opts?: { fraction?: boolean }): string {
  if (value === null) return "—"
  const pct = opts?.fraction ? value * 100 : value
  return `${pct.toFixed(1)}%`
}
