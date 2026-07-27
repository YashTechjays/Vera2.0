import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { FieldRenderer } from "./FieldRenderer"
import type { LeafField } from "@/lib/ibv/types"

// VR2-91: a text field with special_values (e.g. cycle_limit's "No Limit") gets a
// <datalist>, and Chrome paints a dropdown arrow on the input — so a filled field
// reads as an empty dropdown. Suggestions belong only on an EMPTY field.
describe("FieldRenderer text-field suggestions", () => {
  const cycleLimit: LeafField = {
    type: "text",
    title: "Cycle Limit",
    role: "ask",
    special_values: ["No Limit"],
  }

  const render = (value: string) =>
    renderToStaticMarkup(
      <FieldRenderer
        field={cycleLimit}
        path="sections.infertility_treatment.iui.cycle_limit"
        value={value}
        onChange={() => {}}
      />
    )

  it("shows suggestions while the field is empty", () => {
    const html = render("")
    expect(html).toContain("<datalist")
    expect(html).toContain("No Limit")
    expect(html).toContain("list=")
  })

  it("drops the datalist (and its dropdown arrow) once a value is present", () => {
    const html = render("No cycle limit")
    expect(html).not.toContain("<datalist")
    expect(html).not.toContain("list=")
    expect(html).toContain("No cycle limit")
  })
})
