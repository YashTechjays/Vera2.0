// Login-session notification SSE client. One connection for the whole session
// (mounted by NotificationsProvider); the server filters by audience, so every
// event that arrives here is addressed to this user. Reconnects forever with
// capped backoff — the stream tails from "now", so there is no replay to
// discard; consumers refetch current state via the REST API instead (the SSE is
// an accelerant, never the source of truth). Mirrors callEvents.ts transport.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type AppNotification = { type: string; data: Record<string, unknown>; ts: number }

/** A "call needs intervention" alert. `reason` is deliberately not surfaced
 *  here — it can carry PHI; the toast shows category + score only. */
export type InterventionNeeded = { callId: string; score: number; flag: string }

/** Narrow a notification to an intervention alert; null for other/malformed types. */
export function asInterventionNeeded(n: AppNotification): InterventionNeeded | null {
  if (n.type !== "intervention_needed") return null
  const { call_id, score, flag } = n.data as {
    call_id?: unknown
    score?: unknown
    flag?: unknown
  }
  if (typeof call_id !== "string" || typeof score !== "number" || typeof flag !== "string")
    return null
  return { callId: call_id, score, flag }
}

/**
 * Stream the caller's notifications until `signal` aborts. Transient failures
 * (network, 5xx, idle-timeout closes) reconnect with capped backoff;
 * non-retryable request failures (4xx: session expired, permission revoked)
 * throw an ApiError so the caller can stop for good.
 */
export async function streamNotifications(opts: {
  signal: AbortSignal
  onNotification: (n: AppNotification) => void
}): Promise<void> {
  let consecutiveFailures = 0
  for (;;) {
    try {
      await streamOnce(opts.signal, opts.onNotification)
      consecutiveFailures = 0
    } catch (err) {
      if (opts.signal.aborted) return
      if (err instanceof ApiError && err.httpStatus < 500) throw err
      consecutiveFailures += 1
    }
    if (opts.signal.aborted) return
    await backoffDelay(Math.min(1000 * 2 ** consecutiveFailures, 15_000), opts.signal)
  }
}

async function streamOnce(
  signal: AbortSignal,
  onNotification: (n: AppNotification) => void,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/notifications/stream`, {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
    signal,
  })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, null, `notification stream failed (${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split("\n\n")
    buffer = frames.pop() ?? ""
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"))
      if (!dataLine) continue
      const json = dataLine.slice(5).trim()
      if (json) onNotification(JSON.parse(json) as AppNotification)
    }
  }
}

function backoffDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      signal.removeEventListener("abort", finish)
      resolve()
    }
    const timer = setTimeout(finish, ms)
    signal.addEventListener("abort", finish)
  })
}
