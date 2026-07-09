import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { PromptTextarea } from "./PromptTextarea"
import type { PlaceholderGroups } from "@/lib/prompts/document"

const groups: PlaceholderGroups = {
  system: [{ token: "member_id", detail: "sections.basics.plan_type" }],
  context: [{ token: "sections.info.name", detail: "Name" }],
}

describe("PromptTextarea", () => {
  it("renders label, help, value and the picker trigger", () => {
    const html = renderToStaticMarkup(
      <PromptTextarea
        id="t1"
        label="Persona"
        help="Who the agent is."
        value="You are VERA."
        errors={[]}
        groups={groups}
        onChange={() => undefined}
      />,
    )
    expect(html).toContain("Persona")
    expect(html).toContain("Who the agent is.")
    expect(html).toContain("You are VERA.")
    expect(html).toContain("Insert placeholder")
  })

  it("renders inline errors and marks the textarea invalid", () => {
    const html = renderToStaticMarkup(
      <PromptTextarea
        id="t1"
        label="Persona"
        help=""
        value="Hi {{ghost}}"
        errors={["unknown placeholder {{ghost}}"]}
        groups={groups}
        onChange={() => undefined}
      />,
    )
    expect(html).toContain("unknown placeholder")
    expect(html).toContain('aria-invalid="true"')
  })
})
