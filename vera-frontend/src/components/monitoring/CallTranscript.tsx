import { useEffect, useRef, useState } from "react"
import { MessageSquare } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  asCallStatus,
  asTranscriptTurn,
  streamCallEvents,
  type TranscriptTurn,
} from "@/lib/api/callEvents"

/**
 * Live transcript feed for a call, from the /calls/{id}/events SSE.
 * PHI hygiene: turns are tokenized server-side and held in component state only —
 * discarded on unmount (closing the modal). Never persisted or logged.
 * Callers MUST key this component by callId (state resets rely on keyed remount —
 * see both modal call sites).
 */
export function CallTranscript({
  callId,
  onCallStatus,
}: {
  callId: string
  /** Fires for every call_status envelope on the stream ("active", "ended", or a
   *  terminal CallStatus value on DB replay) — the modal lifts this into its
   *  call-ended indication. */
  onCallStatus?: (status: string) => void
}) {
  const [turns, setTurns] = useState<TranscriptTurn[]>([])
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // The stream callback must always see the latest handler without re-opening
  // the SSE (the stream effect below deliberately depends on callId only).
  const onCallStatusRef = useRef(onCallStatus)
  useEffect(() => {
    onCallStatusRef.current = onCallStatus
  }, [onCallStatus])

  useEffect(() => {
    const controller = new AbortController()
    streamCallEvents(callId, {
      signal: controller.signal,
      onEvent: (e) => {
        const turn = asTranscriptTurn(e)
        if (turn) setTurns((prev) => [...prev, turn])
        const status = asCallStatus(e)
        if (status) onCallStatusRef.current?.(status)
      },
      // A dropped connection was re-established and the server replays the
      // stream from the start — discard the stale turns; the replay replaces
      // them (and re-delivers any call_status the outage swallowed).
      onReconnect: () => setTurns([]),
    }).catch((err) => {
      if (!controller.signal.aborted)
        setError(err instanceof Error ? err.message : "Transcript unavailable.")
    })
    return () => controller.abort()
  }, [callId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [turns.length])

  if (error) {
    return <p className="p-4 text-sm text-muted-foreground">{error}</p>
  }
  if (turns.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 py-10 text-muted-foreground">
        <MessageSquare className="size-8 opacity-30" />
        <span className="text-sm">Waiting for transcript…</span>
      </div>
    )
  }
  return (
    <div className="flex-1 space-y-2 overflow-y-auto p-4">
      {turns.map((t, i) => (
        <div
          key={`${t.ts}-${i}`}
          className={cn("flex", t.role === "agent" ? "justify-start" : "justify-end")}
        >
          <div
            className={cn(
              "max-w-[85%] rounded-lg px-3 py-2 text-sm",
              t.role === "agent"
                ? "bg-muted text-foreground"
                : "bg-primary/10 text-foreground",
            )}
          >
            <span className="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              {t.role === "agent" ? "Vera" : "Rep"}
            </span>
            {t.text}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
