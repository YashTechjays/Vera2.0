import { Check, RotateCcw, ArrowUpDown } from "lucide-react"

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
import { modeBadgeClass } from "@/lib/patient-forms/display"
import type { FieldProvenance } from "@/lib/patient-forms/types"

/** ✓ apply → ↶ unapply for a disputed field. */
function ApplyButton({
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
        "inline-flex size-[18px] shrink-0 items-center justify-center rounded text-white transition-colors",
        applied ? "bg-[#34B2B2] hover:bg-[#2c9a9a]" : "bg-[#003e64] hover:bg-[#002a45]"
      )}
    >
      {applied ? <RotateCcw className="size-3" /> : <Check className="size-3" />}
    </button>
  )
}

/** ⇄ swap the input value with the prior value. Drawn disabled (never hidden) on an
 *  inapplicable field — swap would write a value the reviewer could not type back
 *  (VR2-166), but an invisible control reads as broken (VR2-162). */
function SwapButton({
  swapped,
  disabled,
  onClick,
}: {
  swapped: boolean
  disabled?: boolean
  onClick: () => void
}) {
  const reason = disabled
    ? "Swap is unavailable while this field is not applicable"
    : "Swap with prior value"
  return (
    <Tooltip>
      {/* Chrome excludes disabled controls from hit-testing, so the SPAN is the hover
          target — same fix as FieldRenderer's withReasonTooltip. */}
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0">
          <button
            type="button"
            onClick={onClick}
            disabled={disabled}
            aria-label={reason}
            className={cn(
              "inline-flex size-5 shrink-0 items-center justify-center rounded-full text-white transition-colors",
              disabled
                ? "cursor-not-allowed bg-gray-400"
                : swapped
                  ? "bg-[#34B2B2] hover:bg-[#2c9a9a]"
                  : "bg-[#003e64] hover:bg-[#002a45]"
            )}
          >
            {/* Vertical: the value sits ABOVE the prior chip, so the swap arrow points at both. */}
            <ArrowUpDown className="size-3" />
          </button>
        </span>
      </TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}

// The full value lives in the hover tooltip, so the chip stays a fixed-size hint.
const BADGE_VALUE_MAX_CHARS = 24

/** A small badge showing the alternative (prior/captured) value, with a tooltip. */
function DisputeBadge({
  value,
  dispute,
  confidence,
  provenance,
  label,
}: {
  value: string
  dispute: Dispute
  confidence: FieldConfidence
  provenance: FieldProvenance | null
  /** which side of the dispute `value` is */
  label: string
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
          // An aria-label REPLACES the button's text, so naming only the widget would drop
          // the value — the one thing on it a reviewer needs read out. Untruncated, since
          // the visible chip clips at BADGE_VALUE_MAX_CHARS.
          aria-label={`${label}: ${value || "empty"}. Dispute details`}
          className="inline-flex min-w-0 max-w-[240px] items-center gap-1 truncate rounded border border-[#93C5FD] bg-[#EFF6FF] px-1.5 py-0.5 text-[11px] text-black"
        >
          <span className="font-medium">{label}:</span>
          <span className="truncate">{shown || "—"}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent>
        <DisputeTooltipBody
          dispute={dispute}
          confidence={confidence}
          provenance={provenance}
        />
      </TooltipContent>
    </Tooltip>
  )
}

type DisputeStripProps = {
  dispute: Dispute
  /** the one score shown for this field — judge verdict, else capture score */
  confidence: FieldConfidence
  provenance: FieldProvenance | null
  flags: DisputeFlags
  onSwap: () => void
  onApply: () => void
  /** false on a disabled input: Swap writes the value, which the reviewer could not type back (VR2-166) */
  canSwap: boolean
}

/**
 * The dispute row drawn on its own line under the value (VR2-162): the alternative-value
 * chip, the ⇅ swap, then the ✓ apply — in reviewer order: swap first, then accept.
 */
export function DisputeStrip({
  dispute,
  confidence,
  provenance,
  flags,
  onSwap,
  onApply,
  canSwap,
}: DisputeStripProps) {
  const label = flags.swapped ? "Captured" : "Prior"
  return (
    // flex-wrap: in the narrowest matrix cells (copay/coinsurance) the buttons drop to
    // a second line instead of being crushed out of view. The swap+apply pair is one
    // non-wrapping unit, so the two buttons never end up on different lines.
    <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 px-[3px] pb-1 pt-0.5">
      <DisputeBadge
        value={badgeValue(dispute, flags)}
        dispute={dispute}
        confidence={confidence}
        provenance={provenance}
        label={label}
      />
      <span className="flex shrink-0 items-center gap-1.5">
        <SwapButton swapped={flags.swapped} disabled={!canSwap} onClick={onSwap} />
        <ApplyButton applied={flags.applied} onClick={onApply} />
      </span>
    </div>
  )
}

/** Tooltip body: confidence chip + which attempt captured it + evidence + reasoning. */
export function DisputeTooltipBody({
  dispute,
  confidence,
  provenance,
}: {
  dispute: Dispute
  confidence: FieldConfidence
  /** The per-field home for attempt attribution; Call History answers the inverse. */
  provenance: FieldProvenance | null
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
        {provenance && (
          <span className="text-[10px] text-muted-foreground">
            Attempt {provenance.attempt}{" "}
            <span className={cn("rounded px-1 py-0.5", modeBadgeClass(provenance.mode))}>
              {provenance.mode}
            </span>
          </span>
        )}
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
