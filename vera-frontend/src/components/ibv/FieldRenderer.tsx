import { cn } from "@/lib/utils"
import { resolveOptions, widgetOf } from "@/lib/ibv/schema"
import type { IbvField } from "@/lib/ibv/types"

type Props = {
  field: IbvField
  path: string
  value: string
  onChange: (value: string) => void
  highlightClass?: string
  /** extra right padding so inline dispute controls don't overlap the text */
  inputPaddingRight?: string
  /** drop the cell's right border (the section frame supplies that edge, so it
   *  would otherwise be a doubled line) — used by the single-column field rows */
  noRightBorder?: boolean
  /** drop ALL of the input's borders — used inside matrix tables where the
   *  collapsed `<td>` border is the single source of truth for every edge */
  borderless?: boolean
}

// smart-caller-fe spreadsheet cell: Calibri 10pt bold, pale-blue bg, teal
// bottom/right borders; focus → white bg + inset blue ring.
const CELL_BASE =
  "block h-full min-h-[24px] w-full rounded-none border-0 px-[3px] py-0 font-ibv text-[13.3px] font-bold text-black outline-none focus:bg-white focus:shadow-[inset_0_0_0_2px_rgba(59,130,246,0.2)]"
const CELL_LOOK = "border-b border-r border-ibv-input-border bg-ibv-input-bg"
const CELL_LOOK_NO_R = "border-b border-ibv-input-border bg-ibv-input-bg"
// No borders — the matrix <td> (collapsed) owns every edge.
const CELL_LOOK_NONE = "bg-ibv-input-bg"

/** Renders just the input control for a field, switching on type/options. */
export function FieldRenderer({
  field,
  value,
  onChange,
  highlightClass,
  inputPaddingRight,
  noRightBorder,
  borderless,
}: Props) {
  const widget = widgetOf(field)
  const options = resolveOptions(field)
  const look =
    highlightClass ??
    (borderless ? CELL_LOOK_NONE : noRightBorder ? CELL_LOOK_NO_R : CELL_LOOK)
  const padStyle = inputPaddingRight
    ? { paddingRight: inputPaddingRight }
    : undefined

  if (field.confirm_only) {
    return (
      <div
        className={cn(
          "flex h-full min-h-[24px] w-full items-center px-[3px] font-ibv text-[13.3px] font-bold text-black",
          look
        )}
      >
        {value || "—"}
      </div>
    )
  }

  // Any field with a fixed option set renders as a "Select…" dropdown (matches
  // the reference, which renders all enum/constraint fields as <select>).
  if (options.length > 0) {
    return (
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={padStyle}
        className={cn(CELL_BASE, look, value ? "" : "text-[#6b7280]")}
      >
        <option value="">Select…</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    )
  }

  if (widget === "textarea") {
    return (
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={padStyle}
        className={cn(
          "block h-full min-h-[44px] w-full resize-none rounded-none border-0 px-[3px] py-0.5 font-ibv text-[13.3px] font-bold leading-tight text-black outline-none focus:bg-white focus:shadow-[inset_0_0_0_2px_rgba(59,130,246,0.2)]",
          look
        )}
      />
    )
  }

  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={padStyle}
      className={cn("truncate", CELL_BASE, look)}
    />
  )
}
