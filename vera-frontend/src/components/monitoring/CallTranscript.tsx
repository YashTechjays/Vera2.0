import { useEffect, useRef, useState } from "react"
import { Hash, Mic, MessageSquare, PenLine } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  asCallHealth,
  asCallStatus,
  asTranscriptTurn,
  streamCallEvents,
  type CallHealth,
  type TranscriptTurn,
  type TranscriptTurnSource,
} from "@/lib/api/callEvents"
import { transcriptText, turnLabel } from "@/lib/monitoring/transcriptText"

type TurnStyle = { onRight: boolean; label: string; bubble: string }
// The supervisor label is snapshotted when the turn arrives so a later intervener
// (lock steal) can't retroactively relabel earlier turns.
type StampedTurn = TranscriptTurn & { supervisorLabel: string }

/** `source` (the actor) sets the side, label, and colour: our side (Vera + supervisor)
 *  on the left, the caller (rep) on the right. */
function turnStyle(source: TranscriptTurnSource, supervisorLabel: string): TurnStyle {
  const label = turnLabel(source, supervisorLabel)
  switch (source) {
    case "bot":
      return { onRight: false, label, bubble: "bg-muted text-foreground" }
    case "supervisor":
      return { onRight: false, label, bubble: "bg-blue-500/10 text-foreground" }
    case "rep":
      return { onRight: true, label, bubble: "bg-primary/10 text-foreground" }
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
  onTextChange,
  onHealth,
  supervisorLabel = "Supervisor",
}: {
  callId: string
  /** Fires for every call_status envelope on the stream ("active", "ended", or a
   *  terminal CallStatus value on DB replay) with the event's timestamp — the
   *  modal lifts this into its call-started timer and call-ended indication. */
  onCallStatus?: (status: string, ts: number) => void
  /** The transcript as plain text, re-emitted per turn — feeds the modal's
   *  copy button. Same PHI hygiene: component state only, gone on unmount. */
  onTextChange?: (text: string) => void
  /** Fires for every health envelope — the modal lifts this into its header badge. */
  onHealth?: (h: CallHealth) => void
  /** Label for supervisor (takeover) turns — the intervener's email when known. */
  supervisorLabel?: string
}) {
  const [turns, setTurns] = useState<StampedTurn[]>([])
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // The stream callback must always see the latest handler/label without re-opening
  // the SSE (the stream effect below deliberately depends on callId only).
  const onCallStatusRef = useRef(onCallStatus)
  const onTextChangeRef = useRef(onTextChange)
  const onHealthRef = useRef(onHealth)
  const supervisorLabelRef = useRef(supervisorLabel)
  useEffect(() => {
    onCallStatusRef.current = onCallStatus
    onTextChangeRef.current = onTextChange
    onHealthRef.current = onHealth
    supervisorLabelRef.current = supervisorLabel
  }, [onCallStatus, onTextChange, onHealth, supervisorLabel])

  useEffect(() => {
    onTextChangeRef.current?.(transcriptText(turns))
  }, [turns])

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
        const health = asCallHealth(e)
        if (health) onHealthRef.current?.(health)
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
        // `role` decides the shape — speech renders as a bubble, a keypad press or a
        // coaching/whisper note (never heard on the call) as a distinct action chip.
        const { onRight, label, bubble } = turnStyle(t.source, t.supervisorLabel)
        const isCoachingNote = t.role === "coaching" || t.role === "whisper"
        return (
          <div key={`${t.ts}-${i}`} className={cn("flex", onRight ? "justify-end" : "justify-start")}>
            {t.role === "dtmf" ? (
              <div className="flex items-center gap-1.5 rounded-full border border-dashed border-muted-foreground/40 px-3 py-1 text-xs text-muted-foreground">
                <Hash className="size-3" aria-hidden />
                <span>
                  {label} pressed {t.text} on the keypad
                </span>
              </div>
            ) : isCoachingNote ? (
              <div className="flex max-w-[85%] items-start gap-1.5 rounded-lg border border-dashed border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-foreground">
                {t.role === "whisper" ? (
                  <Mic className="mt-0.5 size-3.5 shrink-0 text-amber-600" aria-hidden />
                ) : (
                  <PenLine className="mt-0.5 size-3.5 shrink-0 text-amber-600" aria-hidden />
                )}
                <div>
                  <span className="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-amber-700">
                    {label} coaching
                  </span>
                  {t.text}
                </div>
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
