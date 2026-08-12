import { Check, RotateCcw, ArrowLeftRight } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  badgeValue,
  confidenceChipClass,
  confidenceLabel,
  fieldConfidenceLevel,
  type Dispute,
  type DisputeFlags,
  type FieldConfidence,
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

// The full value lives in the hover tooltip, so the chip stays a fixed-size hint.
const BADGE_VALUE_MAX_CHARS = 10

/** A small badge showing the alternative (prior/captured) value, with a tooltip. */
export function DisputeBadge({
  value,
  dispute,
  confidence,
  label = "Prior",
}: {
  value: string
  dispute: Dispute
  confidence: FieldConfidence
  /** "" renders a bare-value chip — the blue styling alone still reads as "prior" */
  label?: string
}) {
  const shown =
    value.length > BADGE_VALUE_MAX_CHARS
      ? `${value.slice(0, BADGE_VALUE_MAX_CHARS)}…`
      : value
  return (
    <Tooltip>
      {/* A button, not a span: with the label-cell (i) tooltip gone this chip is the
          only keyboard path to the evidence, and a span trigger is mouse-only. */}
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label="Dispute details"
          className="inline-flex max-w-[120px] items-center gap-1 truncate rounded border border-[#93C5FD] bg-[#EFF6FF] px-1.5 py-0.5 text-[10px] text-black"
        >
          {label && <span className="font-medium">{label}:</span>}
          <span className="truncate">{shown || "—"}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent>
        <DisputeTooltipBody dispute={dispute} confidence={confidence} />
      </TooltipContent>
    </Tooltip>
  )
}

type DisputeControlsProps = {
  dispute: Dispute
  /** the one score shown for this field — judge verdict, else capture score */
  confidence: FieldConfidence
  flags: DisputeFlags
  /** absolute placement of the cluster inside the input box, vertical alignment included */
  className: string
  onSwap: () => void
  onApply: () => void
  /** false on a disabled input: Swap writes the value, which the reviewer could not type back (VR2-166) */
  canSwap: boolean
}

/**
 * Narrow cells (the 340px rail): swap/apply only — there is no room beside the
 * value for a chip, so the tooltip carries prior/captured/evidence.
 */
export function CompactDisputeControls({
  dispute,
  confidence,
  flags,
  className,
  onSwap,
  onApply,
  canSwap,
}: DisputeControlsProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className={cn("absolute flex items-center gap-0.5", className)}>
          {canSwap && <SwapButton swapped={flags.swapped} onClick={onSwap} />}
          <ApplyButton applied={flags.applied} onClick={onApply} />
        </div>
      </TooltipTrigger>
      <TooltipContent>
        <DisputeTooltipBody dispute={dispute} confidence={confidence} />
      </TooltipContent>
    </Tooltip>
  )
}

/** Wide cells: swap/apply plus an inline chip of the alternative value. */
export function InlineDisputeControls({
  dispute,
  confidence,
  flags,
  className,
  onSwap,
  onApply,
  bareBadge,
  canSwap,
}: DisputeControlsProps & {
  /** drop the chip's label — for the tightest columns (short values) */
  bareBadge?: boolean
}) {
  const label = flags.swapped ? "Captured" : "Prior"
  return (
    <div className={cn("absolute flex items-center gap-1", className)}>
      {canSwap && <SwapButton swapped={flags.swapped} onClick={onSwap} />}
      <ApplyButton applied={flags.applied} onClick={onApply} />
      <DisputeBadge
        value={badgeValue(dispute, flags)}
        dispute={dispute}
        confidence={confidence}
        label={bareBadge ? "" : label}
      />
    </div>
  )
}

/** Tooltip body: confidence chip + evidence + reasoning. */
export function DisputeTooltipBody({
  dispute,
  confidence,
}: {
  dispute: Dispute
  confidence: FieldConfidence
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold",
            confidenceChipClass(fieldConfidenceLevel(confidence))
          )}
        >
          {confidenceLabel(confidence)}
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
