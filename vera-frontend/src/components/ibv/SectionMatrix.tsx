import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { DisputeStrip } from "./DisputeControls"
import {
  confidenceHighlightClass,
  fieldConfidenceLevel,
  type Dispute,
  type DisputeFlags,
} from "@/lib/ibv/disputes"
import { applicabilityReason, isApplicable } from "@/lib/ibv/schema"
import { invalidSeverity } from "@/lib/ibv/validation"
import type { SectionTable, TableCell } from "@/lib/ibv/schema"

const TH = "border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold"

// ICD-10 / CPT cells are static text. min-w (not w): a plain width gets squeezed
// once the value columns' min-widths overflow the container.
const ICD_WIDTH = "min-w-[90px]"
const CPT_WIDTH = "min-w-[110px]"
const VALUE_WIDTH = "min-w-[100px]"

/** The dispute a cell still draws UI for — gate-failed cells included: the backend
 *  still counts their disputes against completion (VR2-166). */
function unresolvedDispute(
  dispute: Dispute | undefined,
  flags: DisputeFlags
): Dispute | null {
  return dispute !== undefined && !flags.applied ? dispute : null
}

/** One editable matrix cell; a disputed cell grows a second line for the dispute
 *  strip instead of overlaying the value (VR2-162). */
function MatrixCell({ cell, rowSpan }: { cell?: TableCell; rowSpan?: number }) {
  const {
    schema,
    values,
    setValue,
    errors,
    disputeFor,
    confidenceFor,
    provenanceFor,
    flagsFor,
    applyDispute,
    swapDispute,
  } = useIbv()

  if (!cell) {
    // no such column for this row — inert cell
    return <td className="border border-ibv-input-border bg-ibv-label-bg/50" rowSpan={rowSpan} />
  }

  const { path, field, gates } = cell
  const value = values[path] ?? ""
  const dispute = disputeFor(path)
  const confidence = confidenceFor(path)
  const provenance = provenanceFor(path)
  const flags = flagsFor(path)
  const applicable = schema !== null && isApplicable(schema, gates, values)
  const disabledReason =
    !applicable && schema !== null ? applicabilityReason(schema, gates, values) : null
  const invalidReason = errors[path]
  const openDispute = unresolvedDispute(dispute, flags)
  const highlightClass = openDispute
    ? confidenceHighlightClass(fieldConfidenceLevel(confidence))
    : undefined
  const isTextarea = field.ui?.widget === "textarea"

  return (
    // The background lives on the td, never the content: a neighboring cell can make
    // the row taller, and a content-level tint would leave white around it (VR2-162).
    // focus-within turns the ENTIRE cell white while editing — one common style.
    <td
      className={cn(
        "border border-ibv-input-border p-0 align-middle",
        openDispute ? (highlightClass ?? "bg-ibv-input-bg") : "bg-ibv-input-bg",
        "focus-within:bg-white"
      )}
      rowSpan={rowSpan}
      data-field-path={path}
    >
      <div
        className={cn(
          "flex flex-col",
          isTextarea ? "min-h-[44px]" : "min-h-[24px]"
        )}
      >
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
            highlightClass="bg-transparent"
            borderless
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
    </td>
  )
}

/**
 * Renders a `ui.layout: "table"` section as a group-per-row matrix: each
 * top-level group is a band of CPT rows (rowspan label + ICD-10), the shared
 * leaf keys are the columns, and group-level leaves (cycle limit, notes) are
 * per-group rowspan cells.
 */
export function SectionMatrix({ table }: { table: SectionTable }) {
  const { schema, values } = useIbv()
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-ibv text-[13.3px] text-black">
        <thead>
          <tr className="text-center">
            <th className={cn("w-[170px]", TH)}>Service</th>
            {table.hasIcd && <th className={cn(ICD_WIDTH, TH)}>ICD-10</th>}
            <th className={cn(CPT_WIDTH, TH)}>CPT Code</th>
            {table.columns.map((c) => (
              <th key={c.key} className={cn(VALUE_WIDTH, TH)}>
                {c.title}
              </th>
            ))}
            {table.extraColumns.map((c) => (
              <th key={c.key} className={TH}>
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.groups.map((group) => {
            const groupApplicable =
              schema !== null && isApplicable(schema, group.gates, values)
            const groupDisabledReason =
              !groupApplicable && schema !== null
                ? applicabilityReason(schema, group.gates, values)
                : null
            return group.rows.map((row, rowIdx) => (
              <tr key={row.path}>
                {rowIdx === 0 && (
                  <td
                    title={groupDisabledReason ?? undefined}
                    className={cn(
                      "w-[170px] border border-ibv-input-border bg-white px-2 py-0.5 align-middle font-semibold text-ibv-label-border",
                      !groupApplicable && "opacity-60"
                    )}
                    rowSpan={group.rows.length}
                  >
                    <span className="min-w-0 break-words">{group.label}</span>
                  </td>
                )}
                {table.hasIcd && rowIdx === 0 && (
                  <td
                    className={cn(
                      "border border-ibv-input-border bg-white px-2 py-0.5 align-middle text-ibv-label-border",
                      ICD_WIDTH,
                      "break-words"
                    )}
                    rowSpan={group.rows.length}
                  >
                    {group.icd10 || "—"}
                  </td>
                )}
                <td
                  className={cn(
                    "border border-ibv-input-border bg-white px-2 py-0.5 text-center text-black",
                    CPT_WIDTH,
                    "break-words"
                  )}
                >
                  {row.label}
                </td>
                {table.columns.map((c) => (
                  <MatrixCell key={c.key} cell={row.cells[c.key]} />
                ))}
                {table.extraColumns.map((c) =>
                  rowIdx === 0 ? (
                    <MatrixCell
                      key={c.key}
                      cell={group.extras[c.key]}
                      rowSpan={group.rows.length}
                    />
                  ) : null
                )}
              </tr>
            ))
          })}
        </tbody>
      </table>
    </div>
  )
}
