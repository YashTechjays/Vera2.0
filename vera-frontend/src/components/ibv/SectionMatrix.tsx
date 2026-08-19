import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { InlineDisputeControls } from "./DisputeControls"
import {
  confidenceHighlightClass,
  fieldConfidenceLevel,
  type Dispute,
  type DisputeFlags,
  type DisputeMap,
} from "@/lib/ibv/disputes"
import { applicabilityReason, isApplicable } from "@/lib/ibv/schema"
import type { SectionTable, TableCell } from "@/lib/ibv/schema"
import type { LeafField } from "@/lib/ibv/types"

const TH = "border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold"

// ICD-10 / CPT cells are static text (never disputed), so they keep one width in
// both flavours instead of widening with the dispute chips. min-w (not w): a plain
// width gets squeezed once the disputed columns' min-widths overflow the container.
const ICD_WIDTH = "min-w-[90px]"
const CPT_WIDTH = "min-w-[110px]"

/** Short values (Yes/No, money, percent, count) read fine as a bare blue chip, so
 *  their cells reserve less room than a labeled "Prior:" chip needs. */
function usesBareBadge(field: LeafField): boolean {
  return ["enum", "currency", "percent", "integer"].includes(field.type)
}

/** Height of the strip a disputed textarea keeps clear above its text. */
const TEXTAREA_STRIP = "28px"

/** Room reserved inside the input so the dispute cluster never covers the value. */
function disputeGutter(field: LeafField): string {
  return usesBareBadge(field) ? "95px" : "150px"
}

/** Bare-chip columns stay narrow; a labeled chip needs the wider value column. */
function isBareBadgeColumn(table: SectionTable, key: string): boolean {
  return table.groups.some((g) =>
    g.rows.some((r) => {
      const field = r.cells[key]?.field
      return field !== undefined && usesBareBadge(field)
    })
  )
}

// Two width flavours. The wide one only exists to fit the inline Prior chip, which
// renders solely on disputed cells — applying it always would make every form scroll
// sideways to reserve room for chips it never draws, so it is gated on dispute presence.
const COMPACT_WIDTHS = {
  value: "min-w-[100px]",
  bareValue: "min-w-[100px]",
  extra: "",
} as const
const WIDE_WIDTHS = {
  value: "min-w-[210px]",
  bareValue: "min-w-[150px]",
  extra: "min-w-[210px]",
} as const

/** A field draws its dispute UI while the dispute is unresolved — gate-failed cells
 *  included: the backend still counts their disputes against completion (VR2-166). */
function showsDispute(dispute: Dispute | undefined, flags: DisputeFlags): boolean {
  return dispute !== undefined && !flags.applied
}

/** What hasDispute reads out of the IBV context. */
type MatrixDisputeContext = {
  disputes: DisputeMap
  flagsFor: (path: string) => DisputeFlags
}

/** Does any cell in this table still draw dispute UI, so the table narrows back as
 *  disputes get resolved? */
// eslint-disable-next-line react-refresh/only-export-components -- pure predicate, unit-tested
export function hasDispute(
  table: SectionTable,
  { disputes, flagsFor }: MatrixDisputeContext
): boolean {
  const disputed = (cell?: TableCell) =>
    cell !== undefined && showsDispute(disputes[cell.path], flagsFor(cell.path))
  return table.groups.some(
    (g) =>
      Object.values(g.extras).some(disputed) ||
      g.rows.some((r) => Object.values(r.cells).some(disputed))
  )
}

/** One editable matrix cell with inline dispute UI and applicability graying. */
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
  const showDispute = showsDispute(dispute, flags)
  const highlightClass = showDispute
    ? confidenceHighlightClass(fieldConfidenceLevel(confidence))
    : undefined
  const isTextarea = field.ui?.widget === "textarea"

  return (
    // The control fills the cell from out of flow, so the spacer carries the row height:
    // the h-px td + h-full child fill trick renders 0-height in Firefox (VR2-115).
    <td
      className="relative border border-ibv-input-border p-0"
      rowSpan={rowSpan}
      data-field-path={path}
    >
      <div className={isTextarea ? "min-h-[44px]" : "min-h-[24px]"} />
      <div className="absolute inset-0">
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
          // a right gutter would indent EVERY line of a textarea, so long-text cells
          // reserve a strip above the text and put the controls there instead
          inputPaddingRight={showDispute && !isTextarea ? disputeGutter(field) : undefined}
          inputPaddingTop={showDispute && isTextarea ? TEXTAREA_STRIP : undefined}
          borderless
        />
        {showDispute && (
          <InlineDisputeControls
            dispute={dispute!}
            confidence={confidence}
            provenance={provenance}
            flags={flags}
            className={isTextarea ? "top-1 right-1" : "top-1/2 right-1 -translate-y-1/2"}
            bareBadge={usesBareBadge(field)}
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
  const { schema, values, disputes, flagsFor } = useIbv()
  // Wide columns keep value + controls + chip on one row (the wrapper scrolls);
  // an undisputed table keeps its natural width.
  const w = hasDispute(table, { disputes, flagsFor }) ? WIDE_WIDTHS : COMPACT_WIDTHS
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-ibv text-[13.3px] text-black">
        <thead>
          <tr className="text-center">
            <th className={cn("w-[170px]", TH)}>Service</th>
            {table.hasIcd && <th className={cn(ICD_WIDTH, TH)}>ICD-10</th>}
            <th className={cn(CPT_WIDTH, TH)}>CPT Code</th>
            {table.columns.map((c) => (
              <th
                key={c.key}
                className={cn(isBareBadgeColumn(table, c.key) ? w.bareValue : w.value, TH)}
              >
                {c.title}
              </th>
            ))}
            {table.extraColumns.map((c) => (
              <th key={c.key} className={cn(w.extra, TH)}>
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
                      "w-[170px] border border-ibv-input-border bg-white px-2 py-0.5 align-top font-semibold text-ibv-label-border",
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
                      "border border-ibv-input-border bg-white px-2 py-0.5 align-top text-ibv-label-border",
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
