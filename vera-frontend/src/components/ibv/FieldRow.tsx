import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { DisputeStrip } from "./DisputeControls"
import {
  confidenceHighlightClass,
  fieldConfidenceLevel,
  unresolvedDispute,
} from "@/lib/ibv/disputes"
import { VALUE_CAP_CLASS } from "@/lib/ibv/layout"
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
    mode,
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
  // VR2-206 (reopened): in create mode the message must be VISIBLE at the field —
  // the red ring + hover tooltip alone read as a generic error.
  const inlineError = mode === "create" ? invalidReason : undefined
  // Voice-call participation tint on the label cell (see UsageLegend).
  const usage = schema ? fieldUsageOf(schema, path, field) : "asked"

  const openDispute = unresolvedDispute(dispute, flags)
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
            // A disputed capped row keeps the CONTROL at 420px (below) but lets the
            // tinted cell span the full row — a capped tint next to the filler read
            // as a half-filled cell (VR2-162).
            capValue && !openDispute ? VALUE_CAP_CLASS : "flex-1",
            // Stacked (disputed) cells carry the cell chrome themselves so the value
            // line and the dispute strip below it read as ONE cell.
            openDispute && [
              "border-b border-ibv-input-border",
              highlightClass ?? "bg-ibv-input-bg",
              // One common style for the ENTIRE cell while editing — no white patch
              // inside a tinted cell (VR2-162).
              "focus-within:bg-white",
            ]
          )}
        >
          {/* flex-1 + items-stretch: the input fills the row even when the label cell is
              taller, so no sliver of the white section bg shows around it (VR2-162). */}
          <div
            className={cn(
              "flex min-w-0 flex-1 items-stretch",
              capValue && VALUE_CAP_CLASS
            )}
          >
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
              disputeTinted={openDispute !== null}
              borderless={openDispute !== null}
              noRightBorder={!capValue}
              countrySelect={schema !== null && phonePaths(schema).has(path)}
            />
          </div>
          {inlineError && (
            <p className="border-b border-ibv-input-border bg-ibv-input-bg px-1 pb-1 pt-0.5 font-ibv text-[11px] leading-tight text-[#b91c1c]">
              {inlineError}
            </p>
          )}
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
        {capValue && !openDispute && (
          <div className="flex-1 border-b border-ibv-input-border bg-ibv-label-bg/40" />
        )}
      </div>
    </div>
  )
}
