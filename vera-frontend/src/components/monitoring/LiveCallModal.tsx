import { useEffect, useRef, useState } from "react"
import {
  Check,
  Copy,
  Maximize2,
  Minimize2,
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { copyText } from "@/lib/clipboard"
import { usePermission } from "@/lib/auth/permissions"
import { ApiError } from "@/lib/api/client"
import { endCall } from "@/lib/api/calls"
import type { CallHealth } from "@/lib/api/callEvents"
import {
  coachingPanelVisible,
  endCallButtonState,
  interveneButtonState,
  shouldAllowClose,
  type LiveCallMode,
  type RoomStatus,
} from "@/lib/monitoring/liveCallView"
import { healthTone, healthToneClass } from "@/lib/monitoring/health"
import { useIbv } from "@/components/ibv/IbvProvider"
import { SchemaForm } from "@/components/ibv/SchemaForm"
import { CallSummaryPanel } from "./CallSummaryPanel"
import { CallTranscript } from "./CallTranscript"
import { CoachingPanel } from "./CoachingPanel"
import { Keypad } from "./Keypad"
import { LiveCallRoom } from "./LiveCallRoom"
import { useCallStatus } from "./useCallStatus"
import { useLiveDuration } from "./useLiveDuration"
import type { LiveCall } from "@/lib/mock-data"

/** Collapsible form panel; loads the call's own form on expand (VR2-64). */
function FormPanel({
  formId,
  progress,
  onExpand,
}: {
  formId: string | undefined
  progress: number
  onExpand: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const { loadFormById, formId: loadedFormId, loading, error, schema } = useIbv()

  function toggleExpanded() {
    // Skip when already loaded: a refetch would wipe live answers applied so far and any edit.
    if (!expanded && formId && formId !== loadedFormId) loadFormById(formId)
    setExpanded((v) => !v)
  }

  return (
    <>
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-white px-4 py-3">
        <button type="button" onClick={toggleExpanded} className="flex items-center gap-3">
          <span className="font-semibold text-foreground">Patient Information Form</span>
          <span className="text-sm font-semibold text-foreground">{progress}%</span>
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
            onClick={toggleExpanded}
            title={expanded ? "Collapse" : "Expand"}
            className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
          >
            {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="overflow-auto rounded-lg border border-border bg-white p-4">
          {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          {!formId && (
            <p className="text-sm text-muted-foreground">No form linked to this call.</p>
          )}
          {/* IbvFormModal's natural width; the box scrolls both ways. */}
          {schema && !loading && !error && (
            <div className="min-w-[1100px]">
              <SchemaForm />
            </div>
          )}
        </div>
      )}
    </>
  )
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
  onCallEnded,
}: {
  call: LiveCall | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onExpand: () => void
  /** SSE reported the call terminal — lets the list reflect it before its next poll */
  onCallEnded?: (callId: string) => void
}) {
  const canIntervene = usePermission("calls:intervene")
  // The app-level provider IbvFormModal also renders from — one form, so live answers show
  // up inline and full-screen alike.
  const { applyLiveAnswer } = useIbv()
  const [mode, setMode] = useState<LiveCallMode>("listen")
  const [roomStatus, setRoomStatus] = useState<RoomStatus | null>(null)
  const [ending, setEnding] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [keypadOpen, setKeypadOpen] = useState(false)
  // Full-width/height presentation of this modal (the header ⛶), not the IBV form.
  const [maximized, setMaximized] = useState(false)
  // Transcript as plain text (PHI: state only, discarded on unmount) + copy feedback.
  const [transcript, setTranscript] = useState("")
  const [transcriptCopied, setTranscriptCopied] = useState(false)
  const copiedTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(copiedTimer.current), [])
  const [rightTab, setRightTab] = useState<"transcript" | "summary">("transcript")
  const [liveHealth, setLiveHealth] = useState<CallHealth | null>(null)
  // Live form completion from the SSE field_answer frames (0-100); null until the
  // first answer, then it drives the progress bar in place of the polled value.
  const [liveCompletion, setLiveCompletion] = useState<number | null>(null)
  // Reset live state during render when the call changes (React's previous-render
  // pattern, mirroring useCallStatus.ts) — an effect-based reset trips react-hooks
  // v6's set-state-in-effect rule. Close still resets it too, below.
  const [healthForCallId, setHealthForCallId] = useState(call?.id)
  if (call?.id !== healthForCallId) {
    setHealthForCallId(call?.id)
    setLiveHealth(null)
    setLiveCompletion(null)
  }
  // Prefer the live SSE completion; fall back to the polled list value until the first frame.
  const progress = Math.round(liveCompletion ?? call?.formProgress ?? 0)

  // Prefer the live SSE score; fall back to the polled list value until the first envelope.
  const healthScore = liveHealth?.score ?? call?.healthScore ?? null

  const { startedAtMs, callEnded: sseEnded, terminalStatus, onCallStatus } = useCallStatus(
    call?.id,
  )
  const duration = useLiveDuration({
    open,
    ended: sseEnded,
    sseMs: startedAtMs,
    startedAt: call?.startedAt,
  })
  // SSE is the sole source of truth for "call ended"; a room "ended" phase is the
  // supervisor's own connection dropping (LiveCallRoom shows a connection-lost state).
  const callEnded = sseEnded

  // The list is poll-driven and its DB status lags the worker's shutdown drain by
  // many seconds (VR2-72) — surface the SSE terminal signal so it updates now.
  const endedCallId = callEnded ? call?.id : undefined
  useEffect(() => {
    if (endedCallId) onCallEnded?.(endedCallId)
  }, [endedCallId, onCallEnded])
  // A tab that lost its seat holds a dead panel — don't close-lock an intervener into it.
  const replaced = roomStatus?.phase === "replaced"
  const closeAllowed = shouldAllowClose(mode, callEnded, false, replaced)
  const intervene = interveneButtonState(canIntervene, roomStatus)
  const endCallState = endCallButtonState(call?.isOwner ?? false, mode === "intervene", roomStatus)
  const canCoach = coachingPanelVisible(canIntervene, call?.isOwner ?? false, callEnded)

  // Tab close / refresh while intervening abandons the call with a silenced agent — warn.
  // (The modal close-lock only covers Esc/overlay/X, not leaving the page.)
  useEffect(() => {
    if (mode !== "intervene") return
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [mode])

  // Reset to listen-only on close; Radix routes Esc/overlay-click here too, so an intervener can't escape until the call ends.
  function handleOpenChange(next: boolean) {
    if (!shouldAllowClose(mode, callEnded, next, replaced)) return
    if (!next) {
      setMode("listen")
      setRoomStatus(null)
      setActionError(null)
      setMaximized(false)
      setTranscript("")
      setTranscriptCopied(false)
      setRightTab("transcript")
      setLiveHealth(null)
      setLiveCompletion(null)
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
        className={cn(
          "flex flex-col gap-0 p-0",
          maximized
            ? "h-[98vh] max-h-[98vh] w-[98vw] max-w-none"
            : "max-h-[92vh] w-[96vw] max-w-[1100px]",
        )}
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
                onClick={() => setMaximized((v) => !v)}
                title={maximized ? "Restore size" : "Expand"}
                className="flex size-8 items-center justify-center rounded-full bg-muted-foreground/80 text-white transition-colors hover:bg-muted-foreground"
              >
                {maximized ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
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
              <div className="text-xs text-muted-foreground">Call Health</div>
              {/* Live reason from the SSE health frames as an accessible hover
                  tooltip (matches the Live Monitoring table's health cell). */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <div
                    className={cn(
                      "font-semibold",
                      liveHealth?.reason && "cursor-default",
                      healthToneClass[healthTone(healthScore)],
                    )}
                  >
                    {healthScore === null ? "Assessing…" : `${healthScore}%`}
                  </div>
                </TooltipTrigger>
                {liveHealth?.reason && (
                  <TooltipContent className="max-w-72">{liveHealth.reason}</TooltipContent>
                )}
              </Tooltip>
            </div>
          </div>
        </div>

        <div className="flex min-h-[360px] flex-1 gap-4 overflow-hidden bg-[#f8f9fa] p-4">
          <div className="flex flex-1 flex-col gap-3 overflow-auto">
            {/* Keyed per call: expand state must not carry from one call to the next. */}
            <FormPanel
              key={call?.id ?? "none"}
              formId={call?.formId}
              progress={progress}
              onExpand={onExpand}
            />

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
            <div className="flex items-center justify-between bg-[#f3f5f7] px-2 py-2">
              <div className="flex items-center gap-1">
                {(
                  [
                    ["transcript", "Transcription"],
                    ["summary", "Summary"],
                  ] as const
                ).map(([tab, label]) => (
                  <button
                    key={tab}
                    type="button"
                    onClick={() => setRightTab(tab)}
                    className={cn(
                      "rounded-md px-3 py-1.5 text-sm font-semibold transition-colors",
                      rightTab === tab
                        ? "bg-white text-foreground shadow-sm"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <button
                type="button"
                disabled={!transcript}
                title={transcriptCopied ? "Copied" : "Copy transcript"}
                aria-label={transcriptCopied ? "Copied" : "Copy transcript"}
                className="mr-1 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40 disabled:hover:bg-transparent"
                onClick={() => {
                  void copyText(transcript).then((ok) => {
                    if (!ok) return
                    setTranscriptCopied(true)
                    window.clearTimeout(copiedTimer.current)
                    copiedTimer.current = window.setTimeout(() => setTranscriptCopied(false), 2000)
                  })
                }}
              >
                {transcriptCopied ? (
                  <Check className="size-4 text-emerald-600" />
                ) : (
                  <Copy className="size-4" />
                )}
              </button>
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
                {/* The transcript stays mounted (hidden) on the Summary tab — its SSE
                    also drives the call timer and call-ended state. */}
                <div
                  className={cn(
                    "flex flex-1 flex-col overflow-hidden",
                    rightTab !== "transcript" && "hidden",
                  )}
                >
                  <CallTranscript
                    key={`t-${call.id}`}
                    callId={call.id}
                    onCallStatus={onCallStatus}
                    onTextChange={setTranscript}
                    onHealth={setLiveHealth}
                    onFieldAnswer={(a) => {
                      if (call.formId) applyLiveAnswer(call.formId, a.fieldPath, a.value, a.dispute)
                      if (a.completionPct !== null) setLiveCompletion(a.completionPct)
                    }}
                    supervisorLabel={roomStatus?.intervenerLabel ?? undefined}
                  />
                  {canCoach && <CoachingPanel callId={call.id} />}
                </div>
                {rightTab === "summary" && <CallSummaryPanel callId={call.id} />}
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
                disabled={ending || endCallState.disabled}
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
            {/* Helper text, not a title: the disabled button swallows hover events. */}
            {!actionError && !callEnded && endCallState.title && (
              <span className="text-sm text-muted-foreground">{endCallState.title}</span>
            )}
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
