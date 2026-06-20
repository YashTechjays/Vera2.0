import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import {
  ApplyButton,
  SwapButton,
  DisputeBadge,
} from "./DisputeControls"
import { badgeValue, confidenceHighlightClass } from "@/lib/ibv/disputes"
import type { SectionMatrix as SectionMatrixModel } from "@/lib/ibv/schema"

/** One editable matrix cell at `${rowPath}.${colKey}` with inline dispute UI. */
function MatrixCell({ rowPath, colKey, field, rowSpan }: {
  rowPath: string
  colKey: string
  field: SectionMatrixModel["columns"][number]["field"]
  rowSpan?: number
}) {
  const {
    values,
    setValue,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
  } = useIbv()
  const path = `${rowPath}.${colKey}`
  const value = values[path] ?? ""
  const dispute = disputeFor(path)
  const flags = flagsFor(path)
  const showDispute = !!dispute && !flags.applied
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined

  return (
    <td className="border border-ibv-input-border p-0 align-middle" rowSpan={rowSpan}>
      <div className="relative">
        <FieldRenderer
          field={field}
          path={path}
          value={value}
          onChange={(v) => setValue(path, v)}
          highlightClass={highlightClass}
          inputPaddingRight={showDispute ? "70px" : undefined}
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

/** Renders a CPT coverage table from a SectionMatrix model. */
export function SectionMatrix({ matrix }: { matrix: SectionMatrixModel }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-ibv text-[13.3px] text-black">
        <thead>
          <tr className="text-center">
            {matrix.showGroupColumn && (
              <th className="w-[170px] border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold">
                {matrix.rowHeader}
              </th>
            )}
            {matrix.hasIcd && (
              <th className="w-[80px] border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold">
                ICD-10
              </th>
            )}
            <th className="w-[120px] border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold">
              {matrix.rowLabelHeader || "Item"}
            </th>
            {matrix.columns.map((c) => (
              <th
                key={c.key}
                className="min-w-[100px] border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold"
              >
                {c.title}
              </th>
            ))}
            {matrix.groupColumns.map((c) => (
              <th
                key={c.key}
                className="border border-ibv-input-border bg-ibv-label-bg px-2 py-0.5 font-bold"
              >
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.groups.map((group) =>
            group.rows.map((row, rowIdx) => (
              <tr key={row.path}>
                {matrix.showGroupColumn && rowIdx === 0 && (
                  <td
                    className="w-[170px] border border-ibv-input-border bg-white px-2 py-0.5 align-top font-semibold text-ibv-label-border"
                    rowSpan={group.rows.length}
                  >
                    <span className="min-w-0 break-words">{group.label}</span>
                  </td>
                )}
                {matrix.hasIcd && rowIdx === 0 && (
                  <td
                    className="w-[80px] border border-ibv-input-border bg-white px-2 py-0.5 align-top text-ibv-label-border"
                    rowSpan={group.rows.length}
                  >
                    {group.icd10 || "—"}
                  </td>
                )}
                <td className={cn("w-[120px] border border-ibv-input-border bg-white px-2 py-0.5 text-center break-words text-black")}>
                  {row.rowLabel}
                </td>
                {matrix.columns.map((c) => (
                  <MatrixCell
                    key={c.key}
                    rowPath={row.path}
                    colKey={c.key}
                    field={c.field}
                  />
                ))}
                {matrix.groupColumns.map((c) =>
                  rowIdx === 0 ? (
                    <MatrixCell
                      key={c.key}
                      rowPath={group.path}
                      colKey={c.key}
                      field={c.field}
                      rowSpan={group.rows.length}
                    />
                  ) : null
                )}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
