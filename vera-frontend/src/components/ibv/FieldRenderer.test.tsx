import type { ComponentProps } from "react"
import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { fireEvent, render } from "@testing-library/react"

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
    const invalid = render("bad", { invalid: true, title: "Enter a valid value" })
    expect(ancestorTags(invalid)).toEqual(ancestorTags(valid))
  })
})

// The renderer stays type-agnostic: it reports "the user left this cell" and the provider
// owns the unit rule. The assertion that it does NOT append "%" itself is deliberate — it
// guards the decision to canonicalize storage rather than decorate at render time, so the
// stored value, the review UI and the xlsx export can never disagree.
describe("FieldRenderer percent field", () => {
  const coinsurance: LeafField = {
    type: "percent",
    title: "Coinsurance (%)",
    role: "ask",
    validation: { range: { min: 0, max: 100 } },
  }
  const PATH = "sections.general_coverage.office_visits.cpt_99211.coinsurance"

  it("renders the stored value verbatim, without appending a sign", () => {
    const html = renderToStaticMarkup(
      <FieldRenderer field={coinsurance} path={PATH} value="20" onChange={() => {}} />
    )
    expect(html).toContain('value="20"')
    expect(html).not.toContain('value="20%"')
  })

  it("keeps the decimal soft-keyboard hint", () => {
    const html = renderToStaticMarkup(
      <FieldRenderer field={coinsurance} path={PATH} value="" onChange={() => {}} />
    )
    expect(html).toContain('inputMode="decimal"')
  })

  it("reports blur through onCommit with the current text", () => {
    const committed: string[] = []
    const { container } = render(
      <FieldRenderer
        field={coinsurance}
        path={PATH}
        value="20"
        onChange={() => {}}
        onCommit={(v) => committed.push(v)}
      />
    )
    fireEvent.blur(container.querySelector("input")!)
    expect(committed).toEqual(["20"])
  })

  it("does not require onCommit — an omitting caller must not throw on blur", () => {
    const { container } = render(
      <FieldRenderer field={coinsurance} path={PATH} value="20" onChange={() => {}} />
    )
    expect(() => fireEvent.blur(container.querySelector("input")!)).not.toThrow()
  })
})
