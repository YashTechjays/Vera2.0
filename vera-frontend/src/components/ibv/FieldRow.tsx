import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import {
  ApplyButton,
  SwapButton,
  DisputeBadge,
} from "./DisputeControls"
import { badgeValue, confidenceHighlightClass } from "@/lib/ibv/disputes"
import { applicabilityReason, fieldUsageOf, isApplicable, isRequired } from "@/lib/ibv/schema"
import { USAGE_META } from "./usageMeta"
import type { Condition, LeafField } from "@/lib/ibv/types"

type Props = {
  field: LeafField
  path: string
  depth: number
  /** applicable_when chain from the section down to this leaf */
  gates: Condition[]
}

/**
 * One dense spreadsheet row (smart-caller-fe `.field-group`): ~180px gray label
 * cell (navy borders) + pale-blue input cell (teal borders) + inline disputes.
 * Inapplicable rows (own or ancestor `applicable_when` false) gray out and show
 * the field's `inapplicable_value`.
 */
export function FieldRow({ field, path, depth, gates }: Props) {
  const {
    schema,
    values,
    setValue,
    errors,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
  } = useIbv()

  const value = values[path] ?? ""
  const dispute = disputeFor(path)
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
          inputPaddingRight={showDispute ? "150px" : undefined}
          noRightBorder
        />
        {showDispute && (
          <div className="absolute top-1/2 right-1.5 flex -translate-y-1/2 items-center gap-1">
            <SwapButton swapped={flags.swapped} onClick={() => swapDispute(path)} />
            <ApplyButton applied={flags.applied} onClick={() => applyDispute(path)} />
            <DisputeBadge
              value={badgeValue(dispute!, flags)}
              dispute={dispute!}
              label={flags.swapped ? "Captured" : "Prior"}
            />
          </div>
        )}
      </div>
    </div>
  )
}
