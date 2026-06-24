import { Section } from "./Section"
import { schema, getSectionMatrix } from "@/lib/ibv/schema"
import type { IbvSection } from "@/lib/ibv/types"

// Two-column division matching smart-caller-fe's ui schema.
const LEFT_TOP = ["patient_information", "insurance_information"]
const RIGHT_TOP = [
  "appointment_information",
  "verification_information",
  "benefit_coverage",
]
// Reference sections, shown at the bottom (teal box, green headers).
const RAIL_ORDER = ["hospital_information", "provider_reference_information"]
// Schema sections intentionally not rendered anywhere in the form.
const HIDDEN = ["insurance_representative"]

// Payer-reference values (carrier / phone / portal) ride in `intake_payload` but
// aren't a form-schema section, so they're rendered from this synthetic section —
// the field paths still resolve against the form values like any other section.
const INSURANCE_REFERENCE_SECTION: IbvSection = {
  section_key: "insurance_reference_information",
  title: "Insurance Reference",
  properties: {
    insurance: { type: "string", title: "Insurance", ui: { widget: "text" } },
    phone_number: { type: "string", title: "Phone Num", ui: { widget: "text" } },
    portal: { type: "string", title: "Portal", ui: { widget: "text" } },
  },
}

/**
 * Layout (matches the reference's wide, horizontally-scrolling form):
 *  - top: two field-row columns (Patient/Insurance | Appt+Verif/Benefit) that
 *    together fill the entire initial view, followed by the Hospital/Provider
 *    Reference/Insurance Reference box at the far right, fully off-screen until
 *    you drag the bottom scrollbar left→right
 *  - below: full-width CPT tables, then remaining field-row sections two-up
 */
export function SchemaForm() {
  const byKey = (k: string) =>
    schema.sections.find((s) => s.section_key === k)
  const pick = (keys: string[]) =>
    keys.map(byKey).filter((s): s is IbvSection => s !== undefined)

  const leftTop = pick(LEFT_TOP)
  const rightTop = pick(RIGHT_TOP)
  const rail = pick(RAIL_ORDER)

  const placed = new Set([...LEFT_TOP, ...RIGHT_TOP, ...RAIL_ORDER, ...HIDDEN])
  const rest = schema.sections.filter((s) => !placed.has(s.section_key))
  const tables = rest.filter((s) => getSectionMatrix(s) !== null)
  const otherRows = rest.filter((s) => getSectionMatrix(s) === null)
  const otherLeft = otherRows.filter((_, i) => i % 2 === 0)
  const otherRight = otherRows.filter((_, i) => i % 2 === 1)

  return (
    <div className="flex flex-col gap-[15px]">
      <div className="flex gap-5">
        {/* Two main columns fill the entire first view; the reference box sits
            beyond them, off-screen until you drag the bottom scrollbar L→R. */}
        <div className="flex min-w-full gap-5">
          <div className="flex flex-1 flex-col gap-[15px]">
            {leftTop.map((s) => (
              <Section key={s.section_key} section={s} />
            ))}
          </div>
          <div className="flex flex-1 flex-col gap-[15px]">
            {rightTop.map((s) => (
              <Section key={s.section_key} section={s} />
            ))}
          </div>
        </div>
        <aside className="flex w-[340px] shrink-0 flex-col gap-2 self-start rounded-lg border-2 border-[#34B2B2] bg-white p-1.5">
          {rail.map((s) => (
            <Section key={s.section_key} section={s} green />
          ))}
          <Section section={INSURANCE_REFERENCE_SECTION} green />
        </aside>
      </div>

      {tables.map((s) => (
        <Section key={s.section_key} section={s} />
      ))}

      {otherRows.length > 0 && (
        <div className="flex gap-5">
          <div className="flex flex-1 flex-col gap-[15px]">
            {otherLeft.map((s) => (
              <Section key={s.section_key} section={s} />
            ))}
          </div>
          <div className="flex flex-1 flex-col gap-[15px]">
            {otherRight.map((s) => (
              <Section key={s.section_key} section={s} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
