// Live call duration — shared by the monitoring table and the call modals.

/** mm:ss elapsed since the call started (— until it has). */
export function elapsed(startedAt: string | number | null | undefined, now: number): string {
  if (startedAt == null) return "—"
  const start = typeof startedAt === "number" ? startedAt : Date.parse(startedAt)
  const secs = Math.max(0, Math.floor((now - start) / 1000))
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
}

/**
 * The moment a call started, in epoch ms — SSE first, polled seed second.
 * The `call_status: "active"` event's ts lands the instant the callee answers;
 * the polled `started_at` covers a call that was already live before the SSE
 * replay arrives (or while no stream is mounted). Null until either knows.
 */
export function startMs(sseMs: number | null, startedAt: string | null | undefined): number | null {
  if (sseMs != null) return sseMs
  return startedAt ? Date.parse(startedAt) : null
}
