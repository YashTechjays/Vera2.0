import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { DisputeStrip } from "./DisputeControls"
import { confidenceHighlightClass, fieldConfidenceLevel } from "@/lib/ibv/disputes"
import { applicabilityReason, fieldUsageOf, isApplicable } from "@/lib/ibv/schema"
import { invalidSeverity } from "@/lib/ibv/validation"
import { phonePaths } from "@/lib/ibv/phone"
import { USAGE_META } from "./usageMeta"
import type { Condition, LeafField } from "@/lib/ibv/types"

type Props = {
  field: LeafField
  path: string
  depth: number
  /** applicable_when chain from the section down to this leaf */
  gates: Condition[]
  /** cap the value cell's width (table-section leaves span the whole matrix width
   *  otherwise — VR2-162); the leftover space renders as an inert filler */
  capValue?: boolean
}

/**
 * One dense spreadsheet row (smart-caller-fe `.field-group`): ~180px gray label
 * cell (navy borders) + pale-blue input cell (teal borders) + inline disputes.
 * Inapplicable rows (own or ancestor `applicable_when` false) gray out and show
 * the field's `inapplicable_value`.
 */
export function FieldRow({ field, path, depth, gates, capValue }: Props) {
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
  const openDispute = dispute !== undefined && !flags.applied ? dispute : null
  const highlightClass = openDispute
    ? confidenceHighlightClass(fieldConfidenceLevel(confidence))
    : undefined

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
      </div>

      <div className="flex min-w-0 flex-1 items-stretch">
        <div
          className={cn(
            "flex min-w-0 flex-col",
            capValue ? "w-[420px] shrink-0" : "flex-1",
            // Stacked (disputed) cells carry the cell chrome themselves so the value
            // line and the dispute strip below it read as ONE cell.
            openDispute && [
              "border-b border-ibv-input-border",
              capValue && "border-r",
              highlightClass ?? "bg-ibv-input-bg",
              // One common style for the ENTIRE cell while editing — no white patch
              // inside a tinted cell (VR2-162).
              "focus-within:bg-white",
            ]
          )}
        >
          {/* flex-1 + items-stretch: the input fills the row even when the label cell is
              taller, so no sliver of the white section bg shows around it (VR2-162). */}
          <div className="flex min-w-0 flex-1 items-stretch">
            <FieldRenderer
              field={field}
              path={path}
              value={value}
              onChange={(v) => setValue(path, v)}
              disabled={!applicable}
              placeholder={!applicable ? field.inapplicable_value : undefined}
              title={disabledReason ?? invalidReason}
              invalid={invalidSeverity(invalidReason, value)}
              highlightClass={openDispute ? "bg-transparent" : highlightClass}
              borderless={openDispute !== null}
              noRightBorder={!capValue}
              countrySelect={schema !== null && phonePaths(schema).has(path)}
            />
          </div>
          {openDispute && (
            <DisputeStrip
              dispute={openDispute}
              confidence={confidence}
              provenance={provenance}
              flags={flags}
              canSwap={applicable}
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          )}
        </div>
        {capValue && (
          <div className="flex-1 border-b border-ibv-input-border bg-ibv-label-bg/40" />
        )}
      </div>
    </div>
  )
}
