import { MAX_ELEVATION_MINUTES } from "@/lib/api/platform"

/** Parse the duration field's raw text into whole minutes, or null when it isn't
 * a whole number in [1, MAX_ELEVATION_MINUTES]. */
export function parseDurationMinutes(raw: string): number | null {
  const minutes = Number(raw)
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > MAX_ELEVATION_MINUTES) return null
  return minutes
}
