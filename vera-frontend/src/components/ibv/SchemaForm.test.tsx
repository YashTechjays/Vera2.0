import { describe, expect, it, vi } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

// The provider's API-client import reads sessionStorage at module load; give
// the node test environment a minimal in-memory stand-in before imports run.
vi.hoisted(() => {
  const store = new Map<string, string>()
  globalThis.sessionStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size
    },
  } as Storage
})

import { IbvProvider } from "./IbvProvider"
import { SchemaForm } from "./SchemaForm"
import { sectionEntriesOf } from "@/lib/ibv/schema"
import { demoSchema } from "@/lib/ibv/mock"

// Render smoke test: the whole form (all 23 sections, all ui.layout tables)
// must render from a fetched-shape v2 document without throwing. The provider
// is seeded with the dev-fixture schema exactly as openForm (demo) does.
describe("SchemaForm", () => {
  const html = renderToStaticMarkup(
    <IbvProvider initialSchema={demoSchema}>
      <SchemaForm />
    </IbvProvider>
  )

  it("renders every section title from the schema", () => {
    for (const [, section] of sectionEntriesOf(demoSchema)) {
      expect(html).toContain(section.title.replace(/&/g, "&amp;"))
    }
  })

  it("renders the formerly hidden/synthetic sections from the schema itself", () => {
    expect(html).toContain("Insurance Representative")
    expect(html).toContain("Insurance Reference Information")
  })

  it("renders per-CPT matrix rows and group extras for table sections", () => {
    expect(html).toContain("CPT 58323") // IUI row (infertility_treatment table)
    expect(html).toContain("CPT 99211") // office visits row (general_coverage table)
    expect(html).toContain("Cycle Limit") // group-level extra column
    expect(html).toContain("Z31.89") // ICD-10 rowspan cell
  })

  it("marks statically required fields and disables inapplicable ones", () => {
    expect(html).toContain("*")
    // male_partner_coverage is inapplicable with no values → disabled controls
    expect(html).toContain("disabled")
  })

  it("no longer renders the per-row provenance icon", () => {
    // Evidence has one surface now — the dispute tooltip, fed by the field-level
    // `evidence` the backend merges. The label-cell (i) tooltip must not creep back.
    expect(html).not.toContain("Field provenance")
  })

  it("renders the dynamic color legend and the usage tints", () => {
    expect(html).toContain("Color Legend")
    expect(html).toContain("System field")
    expect(html).toContain("Voice-agent context")
    expect(html).toContain("Collected on the call")
    expect(html).toContain("UI only")
    expect(html).toContain("Context section")
    expect(html).toContain("bg-violet-100") // system-field label tint in use
  })
})
