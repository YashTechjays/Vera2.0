import { useState } from "react"
import {
  Maximize2,
  X,
  Volume2,
  Grid3x3,
  Loader2,
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
import { SchemaForm } from "@/components/ibv/SchemaForm"
import { Keypad } from "./Keypad"
import { LiveCallRoom } from "./LiveCallRoom"
import { CallTranscript } from "./CallTranscript"
import type { LiveCall } from "@/lib/mock-data"

function confidenceColor(score: number): string {
  if (score >= 85) return "text-emerald-600"
  if (score >= 70) return "text-amber-600"
  return "text-red-600"
}

/**
 * Live-call detail overview (matches smart-caller-fe's ViewLiveModal):
 *  - left: collapsible "Patient Information Form" summary + call controls
 *  - right: live transcripts
 *  - footer: End Call · Intervene · Show Summary
 * Intervene opens the next (intervention) modal; the maximize icon opens the
 * full Patient Information form.
 */
export function CallOverviewModal({
  call,
  open,
  onOpenChange,
  onExpand,
  onIntervene,
  onEndCall,
  ending,
  onShowSummary,
}: {
  call: LiveCall | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onExpand: () => void
  onIntervene: () => void
  /** Ends the call for real (backend room teardown), not just closes the modal. */
  onEndCall: () => void
  /** True while the end-call request is in flight — disables the button. */
  ending: boolean
  onShowSummary?: () => void
}) {
  const [keypadOpen, setKeypadOpen] = useState(false)
  const [formExpanded, setFormExpanded] = useState(false)
  const progress = call?.formProgress ?? 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
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
                onClick={() => onOpenChange(false)}
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
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
                  title="Audio"
                >
                  <Volume2 className="size-4" />
                </button>
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
          </div>

          {/* Right — live transcripts */}
          <div className="flex flex-1 flex-col overflow-hidden rounded-lg border border-border bg-white">
            <div className="flex items-center justify-between bg-[#f3f5f7] px-4 py-3">
              <h3 className="font-semibold text-foreground">Live Transcripts</h3>
              <Button variant="outline" size="sm" className="gap-1.5">
                <Copy className="size-3.5" />
                Copy
              </Button>
            </div>
            {call?.id ? (
              <div className="flex min-h-0 flex-1 flex-col">
                <div className="shrink-0 border-b border-border">
                  <LiveCallRoom key={call.id} callId={call.id} />
                </div>
                <CallTranscript key={`t-${call.id}`} callId={call.id} />
              </div>
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
            onClick={onEndCall}
            disabled={ending}
            className="bg-red-500 text-white hover:bg-red-600"
          >
            {ending && <Loader2 className="size-4 animate-spin" />}
            {ending ? "Ending…" : "End Call"}
          </Button>
          <div className="flex items-center gap-3">
            <Button
              onClick={onIntervene}
              className="bg-orange-500 text-white hover:bg-orange-600"
            >
              Intervene
            </Button>
            <Button onClick={onShowSummary}>Show Summary</Button>
          </div>
        </div>

        <Keypad open={keypadOpen} onOpenChange={setKeypadOpen} />
      </DialogContent>
    </Dialog>
  )
}
