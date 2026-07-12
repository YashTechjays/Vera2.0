import { useEffect, useState } from "react"

import { elapsed, startMs } from "@/lib/monitoring/liveTimer"

/**
 * Ticking mm:ss call duration for the monitoring modals. The SSE "active" event
 * (`sseMs`) starts the clock the instant the callee answers; the polled
 * `startedAt` seeds a call already live when the modal opened. It ticks once a
 * second while the modal is open and the call is live, then freezes on a terminal
 * status (the interval stops and `now` holds its last value).
 *
 * `running` is true once a start is known — the modals tint the status dot
 * emerald (live) rather than amber (still dialing).
 */
export function useLiveDuration({
  open,
  ended,
  sseMs,
  startedAt,
}: {
  open: boolean
  ended: boolean
  sseMs: number | null
  startedAt: string | null | undefined
}): { label: string; running: boolean } {
  const started = startMs(sseMs, startedAt)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!open || ended) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [open, ended])
  return { label: elapsed(started, now), running: started != null }
}
