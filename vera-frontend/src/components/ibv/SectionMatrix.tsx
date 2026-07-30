import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import { CompactDisputeControls, InlineDisputeControls } from "./DisputeControls"
import {
  confidenceHighlightClass,
  type Dispute,
  type DisputeFlags,
  type DisputeMap,
} from "@/lib/ibv/disputes"
import { applicabilityReason, isApplicable } from "@/lib/ibv/schema"
import type { SectionTable, TableCell } from "@/lib/ibv/schema"
import type { FormSchema, FormValues, LeafField } from "@/lib/ibv/types"

const TH = "border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold"

/** Room reserved inside the input so the dispute cluster never covers the value. */
function disputeGutter(field: LeafField): string {
  // textarea padding applies to EVERY line, so long-text cells reserve buttons only
  if (field.ui?.widget === "textarea") return "50px"
  if (field.type === "enum") return "90px"
  return "150px"
}

/** Yes/No columns need less room than text/money for value + controls + chip. */
function isEnumColumn(table: SectionTable, key: string): boolean {
  return table.groups.some((g) => g.rows.some((r) => r.cells[key]?.field.type === "enum"))
}

// Two width flavours. The wide one only exists to fit the inline Prior chip, which
// renders solely on disputed cells — applying it always would make every form scroll
// sideways to reserve room for chips it never draws, so it is gated on dispute presence.
const COMPACT_WIDTHS = {
  icd: "w-[80px]",
  icdCell: "w-[80px]",
  cpt: "w-[120px]",
  cptCell: "w-[120px] break-words",
  value: "min-w-[100px]",
  enumValue: "min-w-[100px]",
  extra: "",
} as const
const WIDE_WIDTHS = {
  icd: "min-w-[110px]",
  icdCell: "min-w-[110px] whitespace-nowrap",
  cpt: "min-w-[150px]",
  cptCell: "min-w-[150px] whitespace-nowrap",
  value: "min-w-[210px]",
  enumValue: "min-w-[120px]",
  extra: "min-w-[210px]",
} as const

/** A field draws its dispute UI only while the dispute is unresolved and the cell applies. */
function showsDispute(
  dispute: Dispute | undefined,
  flags: DisputeFlags,
  applicable: boolean
): boolean {
  return dispute !== undefined && !flags.applied && applicable
}

/** What hasDispute reads out of the IBV context. */
type MatrixDisputeContext = {
  disputes: DisputeMap
  flagsFor: (path: string) => DisputeFlags
  schema: FormSchema | null
  values: FormValues
}

/** Does any cell in this table still draw dispute UI, so the table narrows back as
 *  disputes get resolved? */
// eslint-disable-next-line react-refresh/only-export-components -- pure predicate, unit-tested
export function hasDispute(
  table: SectionTable,
  { disputes, flagsFor, schema, values }: MatrixDisputeContext
): boolean {
  const disputed = (cell?: TableCell) =>
    cell !== undefined &&
    showsDispute(
      disputes[cell.path],
      flagsFor(cell.path),
      schema !== null && isApplicable(schema, cell.gates, values)
    )
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
  const flags = flagsFor(path)
  const applicable = schema !== null && isApplicable(schema, gates, values)
  const disabledReason =
    !applicable && schema !== null ? applicabilityReason(schema, gates, values) : null
  const invalidReason = errors[path]
  const showDispute = showsDispute(dispute, flags, applicable)
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined
  const isTextarea = field.ui?.widget === "textarea"

  return (
    // h-px is the table-cell "fill height" trick: the cell collapses to its real
    // (possibly row-spanned) height, and the h-full child then stretches to it,
    // so the input covers the whole cell with no top/bottom background strip.
    <td
      className="h-px border border-ibv-input-border p-0"
      rowSpan={rowSpan}
      data-field-path={path}
    >
      <div className="relative h-full">
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
          inputPaddingRight={showDispute ? disputeGutter(field) : undefined}
          borderless
        />
        {showDispute &&
          (isTextarea ? (
            // Buttons pinned to the first line; a 10-char chip of a paragraph is noise.
            <CompactDisputeControls
              dispute={dispute!}
              flags={flags}
              className="top-1 right-1"
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          ) : (
            <InlineDisputeControls
              dispute={dispute!}
              flags={flags}
              className="right-1"
              bareBadge={field.type === "enum"}
              onSwap={() => swapDispute(path)}
              onApply={() => applyDispute(path)}
            />
          ))}
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
  const w = hasDispute(table, { disputes, flagsFor, schema, values })
    ? WIDE_WIDTHS
    : COMPACT_WIDTHS
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-ibv text-[13.3px] text-black">
        <thead>
          <tr className="text-center">
            <th className={cn("w-[170px]", TH)}>Service</th>
            {table.hasIcd && <th className={cn(w.icd, TH)}>ICD-10</th>}
            <th className={cn(w.cpt, TH)}>CPT Code</th>
            {table.columns.map((c) => (
              <th
                key={c.key}
                className={cn(isEnumColumn(table, c.key) ? w.enumValue : w.value, TH)}
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
                      w.icdCell
                    )}
                    rowSpan={group.rows.length}
                  >
                    {group.icd10 || "—"}
                  </td>
                )}
                <td
                  className={cn(
                    "border border-ibv-input-border bg-white px-2 py-0.5 text-center text-black",
                    w.cptCell
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
