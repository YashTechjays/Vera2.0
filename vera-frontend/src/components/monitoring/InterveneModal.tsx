import { useState } from "react"
import {
  X,
  Volume2,
  Grid3x3,
  MessageSquare,
  Copy,
  PhoneOff,
  Save,
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
import type { LiveCall } from "@/lib/mock-data"

type TabKey = "info" | "transcript"

function confidenceColor(score: number): string {
  if (score >= 85) return "text-emerald-600"
  if (score >= 70) return "text-amber-600"
  return "text-red-600"
}

/**
 * Supervisor intervention view (matches smart-caller-fe's InterveneModal):
 * the "next" modal opened from the overview's Intervene button. Shows the
 * editable Patient Information form / live transcripts with Save Form + End Call.
 */
export function InterveneModal({
  call,
  open,
  onOpenChange,
}: {
  call: LiveCall | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [tab, setTab] = useState<TabKey>("info")
  const [keypadOpen, setKeypadOpen] = useState(false)
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
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              title="Close"
              className="flex size-8 items-center justify-center rounded-full bg-muted-foreground/80 text-white transition-colors hover:bg-muted-foreground"
            >
              <X className="size-4" />
            </button>
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

          {/* Form progress */}
          <div className="mt-3">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted-foreground">Form Progress</span>
              <span className="font-medium text-foreground">{progress}%</span>
            </div>
            <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div className="grid grid-cols-2 border-b border-border">
          {(
            [
              { key: "info", label: "Patient Information" },
              { key: "transcript", label: "Live Transcripts" },
            ] as { key: TabKey; label: string }[]
          ).map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={cn(
                "border-b-2 py-2.5 text-center text-sm font-medium transition-colors",
                tab === t.key
                  ? "border-[#34B2B2] text-[#34B2B2]"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="min-h-[320px] flex-1 overflow-auto bg-[#f8f9fa] p-4">
          {tab === "info" ? (
            <SchemaForm />
          ) : (
            <div className="flex flex-col">
              <div className="flex justify-end">
                <Button variant="outline" size="sm" className="gap-1.5">
                  <Copy className="size-3.5" />
                  Copy
                </Button>
              </div>
              {call?.id ? (
                <div className="flex min-h-[240px] flex-1 flex-col rounded-lg border border-border">
                  <LiveCallRoom key={call.id} callId={call.id} microphone />
                </div>
              ) : (
                <div className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
                  <MessageSquare className="size-10 opacity-30" />
                  <span className="text-sm">No call selected</span>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-4 border-t border-border p-4">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-2 text-sm font-semibold tabular-nums">
              <span className="size-2 rounded-full bg-emerald-500" />
              {call?.callTime ?? "00:00"}
            </span>
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
          <div className="flex items-center gap-3">
            <Button className="gap-1.5">
              <Save className="size-4" />
              Save Form
            </Button>
            <Button
              onClick={() => onOpenChange(false)}
              className="gap-1.5 bg-red-500 text-white hover:bg-red-600"
            >
              <PhoneOff className="size-4" />
              End Call
            </Button>
          </div>
        </div>

        <Keypad open={keypadOpen} onOpenChange={setKeypadOpen} />
      </DialogContent>
    </Dialog>
  )
}
