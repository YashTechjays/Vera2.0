import { Info } from "lucide-react"

import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { CompactDisputeControls, InlineDisputeControls } from "./DisputeControls"
import { confidenceHighlightClass } from "@/lib/ibv/disputes"
import { applicabilityReason, fieldUsageOf, isApplicable, isRequired } from "@/lib/ibv/schema"
import { USAGE_META } from "./usageMeta"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { FieldProvenance } from "@/lib/patient-forms/types"
import type { Condition, LeafField } from "@/lib/ibv/types"

/** Tooltip body for AI-sourced field provenance — mirrors DisputeTooltipBody altitude. */
function ProvenanceTooltip({ prov }: { prov: FieldProvenance }) {
  const judgeLabel = prov.judge
    ? ` · judge ${prov.judge.confidence ?? "—"}, ${prov.judge.supported ? "supported" : "unsupported"}`
    : ""

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label="Field provenance"
          className="shrink-0 text-muted-foreground hover:text-foreground"
        >
          <Info className="h-3 w-3" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-[280px]">
        <p className="font-medium">
          Attempt {prov.attempt} ({prov.mode}){judgeLabel}
        </p>
        {prov.judge?.evidence && (
          <p className="mt-1 text-xs text-muted-foreground">"{prov.judge.evidence}"</p>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

type Props = {
  field: LeafField
  path: string
  depth: number
  /** applicable_when chain from the section down to this leaf */
  gates: Condition[]
  /** narrow layout (the 340px rail): controls + badge overflow the ~120px input,
   *  so the badge folds into the tooltip */
  compact?: boolean
}

/**
 * One dense spreadsheet row (smart-caller-fe `.field-group`): ~180px gray label
 * cell (navy borders) + pale-blue input cell (teal borders) + inline disputes.
 * Inapplicable rows (own or ancestor `applicable_when` false) gray out and show
 * the field's `inapplicable_value`.
 */
export function FieldRow({ field, path, depth, gates, compact }: Props) {
  const {
    schema,
    values,
    setValue,
    errors,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
    provenanceFor,
  } = useIbv()

  const value = values[path] ?? ""
  const dispute = disputeFor(path)
  const prov = provenanceFor(path)
  const flags = flagsFor(path)
  const applicable = schema !== null && isApplicable(schema, gates, values)
  const required = schema !== null && applicable && isRequired(schema, field, values)
  const disabledReason =
    !applicable && schema !== null ? applicabilityReason(schema, gates, values) : null
  const invalidReason = errors[path]
  // Voice-call participation tint on the label cell (see UsageLegend).
  const usage = schema ? fieldUsageOf(schema, path, field) : "asked"

  // Highlight + badge only while an unresolved dispute is present.
  const showDispute = !!dispute && !flags.applied && applicable
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined
  const disputeGutter = compact ? "50px" : "150px"

  return (
    <div className="flex min-h-[26px]">
      <div
        className={cn(
          "flex w-[210px] min-w-[210px] shrink-0 items-center gap-1 border-r border-b border-ibv-label-border bg-white px-1.5 py-1 text-left font-ibv text-[13.3px] font-semibold text-ibv-label-border",
          USAGE_META[usage].labelCellClass,
          !applicable && "opacity-60"
        )}
        style={depth > 0 ? { paddingLeft: 6 + depth * 10 } : undefined}
      >
        <span className="min-w-0 flex-1 leading-tight break-words">
          {field.title}
        </span>
        {required && (
          <span className="flex shrink-0 items-center gap-1">
            <span className="text-[#b91c1c]">*</span>
          </span>
        )}
        {prov && <ProvenanceTooltip prov={prov} />}
      </div>

      <div className="relative flex flex-1 items-stretch">
        <FieldRenderer
          field={field}
          path={path}
          value={value}
          onChange={(v) => setValue(path, v)}
          disabled={!applicable}
          placeholder={!applicable ? field.inapplicable_value : undefined}
          title={disabledReason ?? invalidReason}
          invalid={!!invalidReason}
          highlightClass={highlightClass}
          inputPaddingRight={showDispute ? disputeGutter : undefined}
          noRightBorder
        />
        {showDispute &&
          (compact ? (
            <CompactDisputeControls
              dispute={dispute!}
              flags={flags}
              className="top-1/2 right-1 -translate-y-1/2"
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          ) : (
            <InlineDisputeControls
              dispute={dispute!}
              flags={flags}
              className="right-1.5"
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          ))}
      </div>
    </div>
  )
}
