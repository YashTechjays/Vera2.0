import type { ComponentProps } from "react"
import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { FieldRenderer } from "./FieldRenderer"
import type { LeafField } from "@/lib/ibv/types"

// VR2-91: a text field with special_values (e.g. cycle_limit's "No Limit") gets a
// <datalist>, and Chrome paints a picker arrow on such inputs — so a filled field
// read as an unselected dropdown. Hide the ARROW, never the suggestions: dropping
// the datalist on filled fields would kill the feature for every special_values
// field the moment anything is typed.
describe("FieldRenderer text-field suggestions", () => {
  const cycleLimit: LeafField = {
    type: "text",
    title: "Cycle Limit",
    role: "ask",
    special_values: ["No Limit"],
  }

  const render = (
    value: string,
    extra?: Partial<ComponentProps<typeof FieldRenderer>>
  ) =>
    renderToStaticMarkup(
      <FieldRenderer
        field={cycleLimit}
        path="sections.infertility_treatment.iui.cycle_limit"
        value={value}
        onChange={() => {}}
        {...extra}
      />
    )

  it("offers the suggestions while the field is empty", () => {
    const html = render("")
    expect(html).toContain("<datalist")
    expect(html).toContain("No Limit")
    expect(html).toContain("list=")
  })

  it("keeps the suggestions once a value is present", () => {
    const html = render("No cycle limit")
    expect(html).toContain("<datalist")
    expect(html).toContain("No Limit")
    expect(html).toContain("list=")
    expect(html).toContain("No cycle limit")
  })

  it("hides the picker arrow so a filled cell never reads as a dropdown", () => {
    expect(render("No cycle limit")).toContain("no-picker-arrow")
  })

  // A wrapper that appears only when invalid would remount the <input> and drop focus
  // on the very keystroke that flips the field.
  it("keeps the input's ancestor structure stable when validity flips", () => {
    const ancestorTags = (html: string) =>
      [...html.slice(0, html.indexOf("<input")).matchAll(/<([a-zA-Z-]+)/g)].map((m) => m[1])
    const valid = render("No cycle limit")
    const invalid = render("bad", { invalid: "error", title: "Enter a valid value" })
    expect(ancestorTags(invalid)).toEqual(ancestorTags(valid))
  })
})
