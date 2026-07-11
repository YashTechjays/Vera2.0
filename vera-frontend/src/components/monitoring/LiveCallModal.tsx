import { useState } from "react"
import {
  Maximize2,
  X,
  Grid3x3,
  MessageSquare,
  Copy,
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
import { SchemaForm } from "@/components/ibv/SchemaForm"
import { Keypad } from "./Keypad"
import { LiveCallRoom } from "./LiveCallRoom"
import type { LiveCall } from "@/lib/mock-data"

function confidenceColor(score: number): string {
  if (score >= 85) return "text-emerald-600"
  if (score >= 70) return "text-amber-600"
  return "text-red-600"
}

/**
 * The live-call modal: auto-connects listen-only on open, and — for holders of
 * calls:intervene — upgrades in place to a publish-capable connection via
 * Intervene / Stop speaking. The mode is part of LiveCallRoom's key: LiveKit
 * ignores a token swap while connected, so switching modes remounts the room
 * with a freshly minted token.
 *  - left: collapsible "Patient Information Form" summary + call controls
 *  - right: live call panel (connection, participants, audio)
 *  - footer: End Call · Intervene / Stop speaking
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
  const [mode, setMode] = useState<"listen" | "intervene">("listen")
  const [keypadOpen, setKeypadOpen] = useState(false)
  const [formExpanded, setFormExpanded] = useState(false)
  const progress = call?.formProgress ?? 0

  // A fresh open always starts listen-only (the call can't change while open —
  // the worklist is behind the modal), so resetting on close covers every path.
  function handleOpenChange(next: boolean) {
    if (!next) setMode("listen")
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[92vh] w-[96vw] max-w-[1100px] flex-col gap-0 p-0"
      >
        {/* Header */}
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
              <button
                type="button"
                onClick={() => handleOpenChange(false)}
                title="Close"
                className="flex size-8 items-center justify-center rounded-full bg-muted-foreground/80 text-white transition-colors hover:bg-muted-foreground"
              >
                <X className="size-4" />
              </button>
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

        {/* Body — two columns */}
        <div className="flex min-h-[360px] flex-1 gap-4 overflow-hidden bg-[#f8f9fa] p-4">
          {/* Left — form summary + call controls */}
          <div className="flex flex-1 flex-col gap-3 overflow-auto">
            {/* Form summary bar */}
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

            {/* Inline form (when expanded) */}
            {formExpanded && (
              <div className="overflow-auto rounded-lg border border-border bg-white p-4">
                <SchemaForm />
              </div>
            )}

            {/* Call status / controls bar */}
            <div className="flex items-center justify-between rounded-lg border border-border bg-white px-4 py-3">
              <span className="flex items-center gap-2 text-sm font-semibold tabular-nums">
                <span className="size-2 rounded-full bg-amber-500" />
                {call?.callTime ?? "00:00"}
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

          {/* Right — live call panel */}
          <div className="flex flex-1 flex-col overflow-hidden rounded-lg border border-border bg-white">
            <div className="flex items-center justify-between bg-[#f3f5f7] px-4 py-3">
              <h3 className="font-semibold text-foreground">Live Transcripts</h3>
              <Button variant="outline" size="sm" className="gap-1.5">
                <Copy className="size-3.5" />
                Copy
              </Button>
            </div>
            {call?.id ? (
              <LiveCallRoom
                key={`${call.id}:${mode}`}
                callId={call.id}
                microphone={mode === "intervene"}
              />
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
                <MessageSquare className="size-10 opacity-30" />
                <span className="text-sm">No call selected</span>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-4 border-t border-border p-4">
          <Button
            onClick={() => handleOpenChange(false)}
            className="bg-red-500 text-white hover:bg-red-600"
          >
            End Call
          </Button>
          {canIntervene &&
            (mode === "listen" ? (
              <Button
                onClick={() => setMode("intervene")}
                className="bg-orange-500 text-white hover:bg-orange-600"
              >
                Intervene
              </Button>
            ) : (
              <Button variant="outline" onClick={() => setMode("listen")}>
                Stop speaking
              </Button>
            ))}
        </div>

        <Keypad open={keypadOpen} onOpenChange={setKeypadOpen} />
      </DialogContent>
    </Dialog>
  )
}
