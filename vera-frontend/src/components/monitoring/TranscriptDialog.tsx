import { CallTranscript } from "@/components/monitoring/CallTranscript"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { formatDateTime } from "@/lib/patient-forms/display"

export type TranscriptDialogCall = {
  id: string
  patient_name: string | null
  created_at: string
}

/** Read-only popup showing a completed call's transcript, keyed by call id so a
 *  different call remounts CallTranscript (and its session-only turn state). */
export function TranscriptDialog({
  call,
  onOpenChange,
}: {
  call: TranscriptDialogCall | null
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={call !== null} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] flex-col gap-0 p-0 sm:max-w-2xl">
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle>
            Transcript — {call?.patient_name || "—"}
            {call ? ` · ${formatDateTime(call.created_at)}` : ""}
          </DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {call && <CallTranscript key={call.id} callId={call.id} />}
        </div>
      </DialogContent>
    </Dialog>
  )
}
