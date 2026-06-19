import { Check, RotateCcw, ArrowLeftRight } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  confidenceChipClass,
  confidenceLevel,
  type Dispute,
} from "@/lib/ibv/disputes"

/** ✓ apply (teal) → ↶ unapply (green) for a disputed field. */
export function ApplyButton({
  applied,
  onClick,
}: {
  applied: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={applied ? "Unapply" : "Apply captured value"}
      className={cn(
        "inline-flex size-5 items-center justify-center rounded-full text-white transition-colors",
        applied ? "bg-[#34B2B2] hover:bg-[#2c9a9a]" : "bg-[#003e64] hover:bg-[#002a45]"
      )}
    >
      {applied ? <RotateCcw className="size-3" /> : <Check className="size-3" />}
    </button>
  )
}

/** ⇄ swap the input value with the prior value. */
export function SwapButton({
  swapped,
  onClick,
}: {
  swapped: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Swap with prior value"
      className={cn(
        "inline-flex size-5 items-center justify-center rounded-full text-white transition-colors",
        swapped ? "bg-[#34B2B2] hover:bg-[#2c9a9a]" : "bg-[#003e64] hover:bg-[#002a45]"
      )}
    >
      <ArrowLeftRight className="size-3" />
    </button>
  )
}

/** A small badge showing the alternative (prior/captured) value, with a tooltip. */
export function DisputeBadge({
  value,
  dispute,
  label = "Prior",
}: {
  value: string
  dispute: Dispute
  label?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex max-w-[120px] items-center gap-1 truncate rounded border border-[#93C5FD] bg-[#EFF6FF] px-1.5 py-0.5 text-[10px] text-black">
          <span className="font-medium">{label}:</span>
          <span className="truncate">{value || "—"}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <DisputeTooltipBody dispute={dispute} />
      </TooltipContent>
    </Tooltip>
  )
}

/** Tooltip body: confidence chip + evidence + reasoning. */
export function DisputeTooltipBody({ dispute }: { dispute: Dispute }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold",
            confidenceChipClass(dispute.confidence)
          )}
        >
          {dispute.confidence ?? "—"}% · {confidenceLevel(dispute.confidence)}
        </span>
      </div>
      <div>
        <span className="font-medium">Prior:</span> {dispute.previousValue}
      </div>
      <div>
        <span className="font-medium">Captured:</span> {dispute.currentValue}
      </div>
      {dispute.evidence && (
        <div>
          <span className="font-medium">Evidence:</span> {dispute.evidence}
        </div>
      )}
      {dispute.reasoning && (
        <div>
          <span className="font-medium">Reasoning:</span> {dispute.reasoning}
        </div>
      )}
    </div>
  )
}
