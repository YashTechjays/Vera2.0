import { useEffect, useRef, useState } from "react"
import { Hash, MessageSquare } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  asCallStatus,
  asTranscriptTurn,
  streamCallEvents,
  type TranscriptTurn,
  type TranscriptTurnSource,
} from "@/lib/api/callEvents"

type TurnStyle = { onRight: boolean; label: string; bubble: string }
// The supervisor label is snapshotted when the turn arrives so a later intervener
// (lock steal) can't retroactively relabel earlier turns.
type StampedTurn = TranscriptTurn & { supervisorLabel: string }

/** `source` (the actor) sets the side, label, and colour: the caller (rep) on the
 *  left, our side (Vera + supervisor) on the right. */
function turnStyle(source: TranscriptTurnSource, supervisorLabel: string): TurnStyle {
  switch (source) {
    case "bot":
      return { onRight: true, label: "Vera", bubble: "bg-muted text-foreground" }
    case "supervisor":
      return { onRight: true, label: supervisorLabel, bubble: "bg-blue-500/10 text-foreground" }
    case "rep":
      return { onRight: false, label: "Rep", bubble: "bg-primary/10 text-foreground" }
  }
}

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
  supervisorLabel = "Supervisor",
}: {
  callId: string
  /** Fires for every call_status envelope on the stream ("active", "ended", or a
   *  terminal CallStatus value on DB replay) with the event's timestamp — the
   *  modal lifts this into its call-started timer and call-ended indication. */
  onCallStatus?: (status: string, ts: number) => void
  /** Label for supervisor (takeover) turns — the intervener's email when known. */
  supervisorLabel?: string
}) {
  const [turns, setTurns] = useState<StampedTurn[]>([])
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // The stream callback must always see the latest handler/label without re-opening
  // the SSE (the stream effect below deliberately depends on callId only).
  const onCallStatusRef = useRef(onCallStatus)
  const supervisorLabelRef = useRef(supervisorLabel)
  useEffect(() => {
    onCallStatusRef.current = onCallStatus
    supervisorLabelRef.current = supervisorLabel
  }, [onCallStatus, supervisorLabel])

  useEffect(() => {
    const controller = new AbortController()
    streamCallEvents(callId, {
      signal: controller.signal,
      onEvent: (e) => {
        const turn = asTranscriptTurn(e)
        if (turn)
          setTurns((prev) => [...prev, { ...turn, supervisorLabel: supervisorLabelRef.current }])
        const status = asCallStatus(e)
        if (status) onCallStatusRef.current?.(status, e.ts)
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
      {turns.map((t, i) => {
        // `role` decides the shape — speech renders as a bubble, a keypad press as an action chip.
        const { onRight, label, bubble } = turnStyle(t.source, t.supervisorLabel)
        return (
          <div key={`${t.ts}-${i}`} className={cn("flex", onRight ? "justify-end" : "justify-start")}>
            {t.role === "dtmf" ? (
              <div className="flex items-center gap-1.5 rounded-full border border-dashed border-muted-foreground/40 px-3 py-1 text-xs text-muted-foreground">
                <Hash className="size-3" aria-hidden />
                <span>
                  {label} pressed {t.text} on the keypad
                </span>
              </div>
            ) : (
              <div className={cn("max-w-[85%] rounded-lg px-3 py-2 text-sm", bubble)}>
                <span className="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {label}
                </span>
                {t.text}
              </div>
            )}
          </div>
        )
      })}
      <div ref={bottomRef} />
    </div>
  )
}
