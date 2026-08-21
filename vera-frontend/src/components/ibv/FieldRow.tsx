import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { CompactDisputeControls, InlineDisputeControls } from "./DisputeControls"
import { confidenceHighlightClass, fieldConfidenceLevel } from "@/lib/ibv/disputes"
import { applicabilityReason, fieldUsageOf, isApplicable } from "@/lib/ibv/schema"
import { phonePaths } from "@/lib/ibv/phone"
import { USAGE_META } from "./usageMeta"
import type { Condition, LeafField } from "@/lib/ibv/types"

type Props = {
  field: LeafField
  path: string
  depth: number
  /** applicable_when chain from the section down to this leaf */
  gates: Condition[]
  /** narrow layout (the 420px rail): controls + badge overflow the narrow input,
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
    confidenceFor,
    provenanceFor,
    isPathRequired,
  } = useIbv()

  const value = values[path] ?? ""
  const dispute = disputeFor(path)
  const confidence = confidenceFor(path)
  const provenance = provenanceFor(path)
  const flags = flagsFor(path)
  const applicable = schema !== null && isApplicable(schema, gates, values)
  const required = applicable && isPathRequired(path, field)
  const disabledReason =
    !applicable && schema !== null ? applicabilityReason(schema, gates, values) : null
  const invalidReason = errors[path]
  // Voice-call participation tint on the label cell (see UsageLegend).
  const usage = schema ? fieldUsageOf(schema, path, field) : "asked"

  // Drawn even on a gate-failed (grayed) field: the backend still counts its dispute
  // against completion, so hiding the controls would block the form invisibly (VR2-166).
  const showDispute = !!dispute && !flags.applied
  const highlightClass = showDispute
    ? confidenceHighlightClass(fieldConfidenceLevel(confidence))
    : undefined
  const disputeGutter = compact ? "50px" : "150px"

  return (
    <div className="flex min-h-[26px]" data-field-path={path}>
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
        {provenance?.authoritative === false && (
          <span
            className="shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-700"
            title="No call reference — nothing ties this answer to a payer-side record. A reviewer may still accept it."
          >
            Unverified
          </span>
        )}
      </div>

      <div className="relative flex min-w-0 flex-1 items-stretch">
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
          countrySelect={schema !== null && phonePaths(schema).has(path)}
        />
        {showDispute &&
          (compact ? (
            <CompactDisputeControls
              dispute={dispute!}
              confidence={confidence}
              provenance={provenance}
              flags={flags}
              className="top-1/2 right-1 -translate-y-1/2"
              canSwap={applicable}
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          ) : (
            <InlineDisputeControls
              dispute={dispute!}
              confidence={confidence}
              provenance={provenance}
              flags={flags}
              className="top-1/2 right-1.5 -translate-y-1/2"
              canSwap={applicable}
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          ))}
      </div>
    </div>
  )
}
