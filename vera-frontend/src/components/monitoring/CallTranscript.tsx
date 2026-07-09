import { useEffect, useRef, useState } from "react"
import { MessageSquare } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  asTranscriptTurn,
  streamCallEvents,
  type TranscriptTurn,
} from "@/lib/api/callEvents"

/**
 * Live transcript feed for a call, from the /calls/{id}/events SSE.
 * PHI hygiene: turns are tokenized server-side and held in component state only —
 * discarded on unmount (closing the modal). Never persisted or logged.
 */
export function CallTranscript({ callId }: { callId: string }) {
  const [turns, setTurns] = useState<TranscriptTurn[]>([])
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    streamCallEvents(callId, {
      signal: controller.signal,
      onEvent: (e) => {
        const turn = asTranscriptTurn(e)
        if (turn) setTurns((prev) => [...prev, turn])
      },
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
