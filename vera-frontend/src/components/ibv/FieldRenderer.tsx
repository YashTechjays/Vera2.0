import { cn } from "@/lib/utils"
import { optionsOf, suggestionsOf } from "@/lib/ibv/schema"
import type { LeafField } from "@/lib/ibv/types"

type Props = {
  field: LeafField
  path: string
  value: string
  onChange: (value: string) => void
  /** field (or an ancestor) is inapplicable — gray out and lock the control */
  disabled?: boolean
  /** shown when empty; callers pass inapplicable_value for skipped fields */
  placeholder?: string
  /** current value fails client-side validation (pattern/range/required) */
  invalid?: boolean
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
const DISABLED_LOOK = "cursor-not-allowed opacity-60"
const INVALID_LOOK = "shadow-[inset_0_0_0_2px_rgba(239,68,68,0.45)]"

/** Base cell look when no dispute highlight overrides it. */
function baseLook(borderless?: boolean, noRightBorder?: boolean): string {
  if (borderless) return CELL_LOOK_NONE
  if (noRightBorder) return CELL_LOOK_NO_R
  return CELL_LOOK
}

/** Numeric soft-keyboard hint for the currency/percent/integer widgets. */
function inputModeFor(type: LeafField["type"]): "decimal" | "numeric" | undefined {
  switch (type) {
    case "currency":
    case "percent":
      return "decimal"
    case "integer":
      return "numeric"
    default:
      return undefined
  }
}

/** Widget for one leaf field, switched on the DSL `type` (+ `ui.widget`). */
export function FieldRenderer({
  field,
  path,
  value,
  onChange,
  disabled,
  placeholder,
  invalid,
  highlightClass,
  inputPaddingRight,
  noRightBorder,
  borderless,
}: Props) {
  const look = cn(
    highlightClass ?? baseLook(borderless, noRightBorder),
    disabled && DISABLED_LOOK,
    invalid && !disabled && INVALID_LOOK
  )
  const padStyle = inputPaddingRight
    ? { paddingRight: inputPaddingRight }
    : undefined
  // `default` is the value the form assumes when nothing is recorded — surface
  // it (or the skip value for inapplicable fields) without writing a value.
  const hint = placeholder ?? field.default

  if (field.role === "readonly") {
    return (
      <div
        className={cn(
          "flex h-full min-h-[24px] w-full items-center px-[3px] font-ibv text-[13.3px] font-bold text-black",
          look
        )}
      >
        {value || hint || "—"}
      </div>
    )
  }

  if (field.type === "enum") {
    const options = optionsOf(field)
    return (
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        style={padStyle}
        className={cn(CELL_BASE, look, value ? "" : "text-[#6b7280]")}
      >
        <option value="">{hint ?? "Select…"}</option>
        {/* keep a recorded value visible even if it's outside the vocabulary */}
        {value && !options.includes(value) && <option value={value}>{value}</option>}
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    )
  }

  if (field.ui?.widget === "textarea") {
    return (
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={hint}
        style={padStyle}
        className={cn(
          "block h-full min-h-[44px] w-full resize-none rounded-none border-0 px-[3px] py-0.5 font-ibv text-[13.3px] font-bold leading-tight text-black outline-none focus:bg-white focus:shadow-[inset_0_0_0_2px_rgba(59,130,246,0.2)]",
          look
        )}
      />
    )
  }

  const suggestions = suggestionsOf(field)
  const listId = suggestions.length > 0 ? `${path}--suggestions` : undefined
  const inputMode = inputModeFor(field.type)

  return (
    <>
      <input
        type={field.type === "phone" ? "tel" : "text"}
        inputMode={inputMode}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={
          hint ?? (field.type === "date" ? field.validation?.date_format : undefined)
        }
        list={listId}
        style={padStyle}
        className={cn("truncate", CELL_BASE, look)}
      />
      {listId && (
        <datalist id={listId}>
          {suggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
      )}
    </>
  )
}
