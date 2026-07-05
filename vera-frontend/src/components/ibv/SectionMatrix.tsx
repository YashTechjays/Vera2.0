import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import {
  ApplyButton,
  SwapButton,
  DisputeBadge,
} from "./DisputeControls"
import { badgeValue, confidenceHighlightClass } from "@/lib/ibv/disputes"
import { isApplicable } from "@/lib/ibv/schema"
import type { SectionTable, TableCell } from "@/lib/ibv/schema"

const TH = "border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold"

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
  const showDispute = !!dispute && !flags.applied && applicable
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined

  return (
    // h-px is the table-cell "fill height" trick: the cell collapses to its real
    // (possibly row-spanned) height, and the h-full child then stretches to it,
    // so the input covers the whole cell with no top/bottom background strip.
    <td className="h-px border border-ibv-input-border p-0" rowSpan={rowSpan}>
      <div className="relative h-full">
        <FieldRenderer
          field={field}
          path={path}
          value={value}
          onChange={(v) => setValue(path, v)}
          disabled={!applicable}
          placeholder={!applicable ? field.inapplicable_value : undefined}
          invalid={!!errors[path]}
          highlightClass={highlightClass}
          inputPaddingRight={showDispute ? "70px" : undefined}
          borderless
        />
        {showDispute && (
          <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5">
            <SwapButton swapped={flags.swapped} onClick={() => swapDispute(path)} />
            <ApplyButton applied={flags.applied} onClick={() => applyDispute(path)} />
          </div>
        )}
        {showDispute && (
          <div className="px-1 pb-0.5">
            <DisputeBadge
              value={badgeValue(dispute!, flags)}
              dispute={dispute!}
              label={flags.swapped ? "Captured" : "Prior"}
            />
          </div>
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
            {table.hasIcd && <th className={cn("w-[80px]", TH)}>ICD-10</th>}
            <th className={cn("w-[120px]", TH)}>CPT Code</th>
            {table.columns.map((c) => (
              <th key={c.key} className={cn("min-w-[100px]", TH)}>
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
            return group.rows.map((row, rowIdx) => (
              <tr key={row.path}>
                {rowIdx === 0 && (
                  <td
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
                    className="w-[80px] border border-ibv-input-border bg-white px-2 py-0.5 align-top text-ibv-label-border"
                    rowSpan={group.rows.length}
                  >
                    {group.icd10 || "—"}
                  </td>
                )}
                <td className="w-[120px] border border-ibv-input-border bg-white px-2 py-0.5 text-center break-words text-black">
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
