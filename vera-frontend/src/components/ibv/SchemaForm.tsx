import { TriangleAlert } from "lucide-react"

import { Section } from "./Section"
import { UsageLegend } from "./UsageLegend"
import { useIbv } from "./IbvProvider"
import { contradictionWarnings, sectionEntriesOf } from "@/lib/ibv/schema"
import type { FormSchema, Section as SectionModel } from "@/lib/ibv/types"

// Presentation-only placement hints (matches smart-caller-fe's wide form).
// Sections listed here anchor the top block; every other section renders below
// in schema document order (object key order = UI order).
const LEFT_TOP = ["patient_information", "insurance_information"]
const RIGHT_TOP = [
  "appointment_information",
  "verification_information",
  "benefit_coverage",
]
// Reference sections, shown in the teal box at the far right.
const RAIL = [
  "hospital_information",
  "provider_reference_information",
  "insurance_reference_information",
]

type Entry = [string, SectionModel]
type Chunk =
  | { kind: "table"; entry: Entry }
  | { kind: "run"; entries: Entry[] }

/** Split the remaining sections into full-width table chunks and two-up runs,
 *  preserving document order. */
function chunkRest(rest: Entry[]): Chunk[] {
  const out: Chunk[] = []
  let run: Entry[] = []
  for (const entry of rest) {
    if (entry[1].ui?.layout === "table") {
      if (run.length > 0) {
        out.push({ kind: "run", entries: run })
        run = []
      }
      out.push({ kind: "table", entry })
    } else {
      run.push(entry)
    }
  }
  if (run.length > 0) out.push({ kind: "run", entries: run })
  return out
}

/** Amber banner for contradiction rules currently violated by the values. */
function ContradictionBanner({ schema }: { schema: FormSchema }) {
  const { values } = useIbv()
  const warnings = contradictionWarnings(schema, values)
  if (warnings.length === 0) return null
  return (
    <div
      role="alert"
      className="flex flex-col gap-1 rounded-md border border-amber-400 bg-amber-50 px-3 py-2 text-[13px] text-amber-900"
    >
      {warnings.map((w) => (
        <p key={w.rule_key} className="flex items-start gap-2">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" />
          <span>{w.reason}</span>
        </p>
      ))}
    </div>
  )
}

/**
 * Layout (matches the reference's wide, horizontally-scrolling form):
 *  - top: two field-row columns (Patient/Insurance | Appt+Verif/Benefit) that
 *    together fill the entire initial view, followed by the Hospital/Provider
 *    Reference/Insurance Reference box at the far right, fully off-screen until
 *    you drag the bottom scrollbar left→right
 *  - below: the remaining sections in schema document order — `ui.layout:
 *    "table"` sections full-width, runs of field-row sections two-up
 */
export function SchemaForm() {
  // The document the open form is pinned to — fetched from the backend by
  // schema_version_id (IbvProvider); null until it arrives.
  const { schema } = useIbv()
  if (!schema) return null

  const pick = (keys: string[]): Entry[] =>
    keys.flatMap((k) => (schema.sections[k] ? [[k, schema.sections[k]] as Entry] : []))

  const leftTop = pick(LEFT_TOP)
  const rightTop = pick(RIGHT_TOP)
  const rail = pick(RAIL)

  const placed = new Set([...LEFT_TOP, ...RIGHT_TOP, ...RAIL])
  const rest = sectionEntriesOf(schema).filter(([k]) => !placed.has(k))
  const chunks = chunkRest(rest)

  const renderColumn = (entries: Entry[]) => (
    <div className="flex flex-1 flex-col gap-[15px]">
      {entries.map(([key, section]) => (
        <Section key={key} sectionKey={key} section={section} />
      ))}
    </div>
  )

  return (
    <div className="flex flex-col gap-[15px]">
      <ContradictionBanner schema={schema} />
      <div className="flex gap-5">
        {/* Two main columns fill the entire first view; the reference box sits
            beyond them, off-screen until you drag the bottom scrollbar L→R. */}
        <div className="flex min-w-full gap-5">
          {renderColumn(leftTop)}
          {renderColumn(rightTop)}
        </div>
        <aside className="flex w-[340px] shrink-0 flex-col gap-2 self-start rounded-lg border-2 border-[#34B2B2] bg-white p-1.5">
          {rail.map(([key, section]) => (
            <Section key={key} sectionKey={key} section={section} />
          ))}
        </aside>
      </div>

      {chunks.map((chunk, i) =>
        chunk.kind === "run" ? (
          <div key={`run-${i}`} className="flex gap-5">
            {renderColumn(chunk.entries.filter((_, j) => j % 2 === 0))}
            {renderColumn(chunk.entries.filter((_, j) => j % 2 === 1))}
          </div>
        ) : (
          <Section
            key={chunk.entry[0]}
            sectionKey={chunk.entry[0]}
            section={chunk.entry[1]}
          />
        )
      )}

      <UsageLegend schema={schema} />
    </div>
  )
}
