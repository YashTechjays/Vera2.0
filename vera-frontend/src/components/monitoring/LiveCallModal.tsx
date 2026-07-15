import { useState } from "react"
import {
  Maximize2,
  X,
  Grid3x3,
  MessageSquare,
  ChevronDown,
  ChevronUp,
} from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { usePermission } from "@/lib/auth/permissions"
import { ApiError } from "@/lib/api/client"
import { endCall } from "@/lib/api/calls"
import {
  interveneButtonState,
  shouldAllowClose,
  type LiveCallMode,
  type RoomStatus,
} from "@/lib/monitoring/liveCallView"
import { SchemaForm } from "@/components/ibv/SchemaForm"
import { CallTranscript } from "./CallTranscript"
import { Keypad } from "./Keypad"
import { LiveCallRoom } from "./LiveCallRoom"
import { useCallStatus } from "./useCallStatus"
import { useLiveDuration } from "./useLiveDuration"
import type { LiveCall } from "@/lib/mock-data"

function confidenceColor(score: number): string {
  if (score >= 85) return "text-emerald-600"
  if (score >= 70) return "text-amber-600"
  return "text-red-600"
}

/**
 * The live-call modal: auto-connects listen-only, and upgrades in place to publish via
 * Intervene for calls:intervene holders. Mode is part of LiveCallRoom's key — LiveKit
 * ignores a token swap while connected, so a mode switch remounts. Intervening is one-way:
 * no close until the call ends.
 *
 * "Call ended" comes from the events stream's terminal call_status or the room dying
 * (End Call deletes it server-side).
 */
export function LiveCallModal({
  call,
  open,
  onOpenChange,
  onExpand,
}: {
  call: LiveCall | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onExpand: () => void
}) {
  const canIntervene = usePermission("calls:intervene")
  const [mode, setMode] = useState<LiveCallMode>("listen")
  const [roomStatus, setRoomStatus] = useState<RoomStatus | null>(null)
  const [ending, setEnding] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [keypadOpen, setKeypadOpen] = useState(false)
  const [formExpanded, setFormExpanded] = useState(false)
  const progress = call?.formProgress ?? 0

  const { startedAtMs, callEnded: sseEnded, terminalStatus, onCallStatus } = useCallStatus(
    call?.id,
  )
  const duration = useLiveDuration({
    open,
    ended: sseEnded,
    sseMs: startedAtMs,
    startedAt: call?.startedAt,
  })
  const callEnded = sseEnded || roomStatus?.phase === "ended"
  const closeAllowed = shouldAllowClose(mode, callEnded, false)
  const intervene = interveneButtonState(canIntervene, roomStatus)

  // Reset to listen-only on close; Radix routes Esc/overlay-click here too, so an intervener can't escape until the call ends.
  function handleOpenChange(next: boolean) {
    if (!shouldAllowClose(mode, callEnded, next)) return
    if (!next) {
      setMode("listen")
      setRoomStatus(null)
      setActionError(null)
    }
    onOpenChange(next)
  }

  async function handleEndCall() {
    if (!call?.id) return
    setEnding(true)
    try {
      await endCall(call.id)
      // The room is torn down server-side; SSE terminal status / disconnect flips callEnded and unlocks the modal.
      setMode("listen")
      setRoomStatus(null)
      setActionError(null)
      onOpenChange(false)
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : "Could not end the call.")
    } finally {
      setEnding(false)
    }
  }

  // An intervene token can be refused (e.g. 409 if someone took the mic first) — fall back to listening.
  function handleJoinFailed(error: unknown) {
    if (mode !== "intervene") return
    setMode("listen")
    setActionError(error instanceof ApiError ? error.message : "Could not intervene.")
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[92vh] w-[96vw] max-w-[1100px] flex-col gap-0 p-0"
      >
        <div className="border-b border-border p-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <DialogTitle className="text-lg font-semibold">Overview</DialogTitle>
              {call?.id && (
                <p className="mt-0.5 font-mono text-xs text-muted-foreground">Call {call.id}</p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onExpand}
                title="Open full form"
                className="flex size-8 items-center justify-center rounded-full bg-muted-foreground/80 text-white transition-colors hover:bg-muted-foreground"
              >
                <Maximize2 className="size-4" />
              </button>
              {closeAllowed && (
                <button
                  type="button"
                  onClick={() => handleOpenChange(false)}
                  title="Close"
                  className="flex size-8 items-center justify-center rounded-full bg-muted-foreground/80 text-white transition-colors hover:bg-muted-foreground"
                >
                  <X className="size-4" />
                </button>
              )}
            </div>
          </div>

          <div className="mt-2 grid grid-cols-3 gap-4">
            <div>
              <div className="text-xs text-muted-foreground">Patient Name</div>
              <div className="font-semibold">{call?.patient ?? "—"}</div>
            </div>
            <div className="text-center">
              <div className="text-xs text-muted-foreground">Insurance Company</div>
              <div className="font-semibold">{call?.insurance ?? "—"}</div>
            </div>
            <div className="text-right">
              <div className="text-xs text-muted-foreground">Confidence</div>
              <div className={cn("font-semibold", confidenceColor(call?.confidence ?? 0))}>
                {call?.confidence ?? 0}%
              </div>
            </div>
          </div>
        </div>

        <div className="flex min-h-[360px] flex-1 gap-4 overflow-hidden bg-[#f8f9fa] p-4">
          <div className="flex flex-1 flex-col gap-3 overflow-auto">
            <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-white px-4 py-3">
              <button
                type="button"
                onClick={() => setFormExpanded((v) => !v)}
                className="flex items-center gap-3"
              >
                <span className="font-semibold text-foreground">
                  Patient Information Form
                </span>
                <span className="text-sm font-semibold text-foreground">
                  {progress}%
                </span>
              </button>
              <div className="flex items-center gap-2">
                <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-primary transition-all"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <button
                  type="button"
                  onClick={onExpand}
                  title="Open full form"
                  className="flex size-7 items-center justify-center rounded-md text-foreground hover:bg-muted"
                >
                  <Maximize2 className="size-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setFormExpanded((v) => !v)}
                  title={formExpanded ? "Collapse" : "Expand"}
                  className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
                >
                  {formExpanded ? (
                    <ChevronUp className="size-4" />
                  ) : (
                    <ChevronDown className="size-4" />
                  )}
                </button>
              </div>
            </div>

            {formExpanded && (
              <div className="overflow-auto rounded-lg border border-border bg-white p-4">
                <SchemaForm />
              </div>
            )}

            {/* Timer starts on the SSE "active" event (callee answered) and freezes on a terminal status. */}
            <div className="flex items-center justify-between rounded-lg border border-border bg-white px-4 py-3">
              <span className="flex items-center gap-2 text-sm font-semibold tabular-nums">
                <span
                  className={cn(
                    "size-2 rounded-full",
                    duration.running && !callEnded ? "bg-emerald-500" : "bg-amber-500",
                  )}
                />
                {duration.label}
              </span>
              <button
                type="button"
                onClick={() => setKeypadOpen(true)}
                className="flex size-8 items-center justify-center rounded-md text-foreground hover:bg-muted"
                title="Keypad"
              >
                <Grid3x3 className="size-4" />
              </button>
            </div>
          </div>

          <div className="flex flex-1 flex-col overflow-hidden rounded-lg border border-border bg-white">
            <div className="flex items-center justify-between bg-[#f3f5f7] px-4 py-3">
              <h3 className="font-semibold text-foreground">Live Transcripts</h3>
            </div>
            {call?.id ? (
              <div className="flex flex-1 flex-col overflow-hidden">
                <LiveCallRoom
                  key={`${call.id}:${mode}`}
                  callId={call.id}
                  microphone={mode === "intervene"}
                  ended={sseEnded}
                  endedStatus={terminalStatus}
                  onStatus={setRoomStatus}
                  onJoinFailed={handleJoinFailed}
                />
                <CallTranscript
                  key={`t-${call.id}`}
                  callId={call.id}
                  onCallStatus={onCallStatus}
                />
              </div>
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
                <MessageSquare className="size-10 opacity-30" />
                <span className="text-sm">No call selected</span>
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 border-t border-border p-4">
          <div className="flex items-center gap-3">
            {!callEnded && (
              <Button
                onClick={() => void handleEndCall()}
                disabled={ending}
                className="bg-red-500 text-white hover:bg-red-600"
              >
                {ending ? "Ending…" : "End Call"}
              </Button>
            )}
            {(mode === "listen" || callEnded) && (
              <Button variant="outline" onClick={() => handleOpenChange(false)}>
                Close
              </Button>
            )}
            {actionError && <span className="text-sm text-destructive">{actionError}</span>}
          </div>
          {intervene.visible && mode === "listen" && !callEnded && (
            <Button
              onClick={() => {
                setActionError(null)
                setMode("intervene")
              }}
              disabled={intervene.disabled}
              title={intervene.title}
              className="bg-orange-500 text-white hover:bg-orange-600"
            >
              Intervene
            </Button>
          )}
        </div>

        <Keypad open={keypadOpen} onOpenChange={setKeypadOpen} />
      </DialogContent>
    </Dialog>
  )
}
