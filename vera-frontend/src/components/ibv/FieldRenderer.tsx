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
}

// smart-caller-fe spreadsheet cell: Calibri 10pt bold, pale-blue bg, teal
// bottom/right borders; focus → white bg + inset blue ring.
const CELL_BASE =
  "h-[22px] w-full rounded-none border-0 px-[3px] py-0 font-ibv text-[13.3px] font-bold text-black outline-none focus:bg-white focus:shadow-[inset_0_0_0_2px_rgba(59,130,246,0.2)]"
const CELL_LOOK = "border-b border-r border-ibv-input-border bg-ibv-input-bg"

/** Renders just the input control for a field, switching on type/options. */
export function FieldRenderer({
  field,
  value,
  onChange,
  highlightClass,
  inputPaddingRight,
}: Props) {
  const widget = widgetOf(field)
  const options = resolveOptions(field)
  const look = highlightClass ?? CELL_LOOK
  const padStyle = inputPaddingRight
    ? { paddingRight: inputPaddingRight }
    : undefined

  if (field.confirm_only) {
    return (
      <div
        className={cn(
          "flex h-[22px] items-center px-[3px] font-ibv text-[13.3px] font-bold text-black",
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
          "min-h-[44px] w-full resize-none rounded-none border-0 px-[3px] py-0.5 font-ibv text-[13.3px] font-bold leading-tight text-black outline-none focus:bg-white focus:shadow-[inset_0_0_0_2px_rgba(59,130,246,0.2)]",
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
