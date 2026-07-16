import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { PromptTextarea } from "./PromptTextarea"
import { OverrideFieldRow } from "./OverrideFieldRow"
import { PreviewPane } from "./PreviewPane"
import { VersionList } from "./VersionList"
import type { PlaceholderGroups } from "@/lib/prompts/document"
import type { PromptVersionSummary } from "@/lib/api/prompts"

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

describe("OverrideFieldRow", () => {
  const base = {
    taskKey: "wrap_up",
    field: "outro" as const,
    label: "Outro",
    help: "Spoken verbatim when the task completes.",
    errors: [] as string[],
    groups,
    onChange: () => undefined,
    onOverride: () => undefined,
    onReset: () => undefined,
  }

  it("default state: read-only default text + Override action", () => {
    const html = renderToStaticMarkup(
      <OverrideFieldRow {...base} state="default" value="" defaultText="Goodbye now." />,
    )
    expect(html).toContain("Schema default")
    expect(html).toContain("Goodbye now.")
    expect(html).toContain("Override")
    expect(html).not.toContain("Reset to default")
  })

  it("no-default state: Add action, no default text block", () => {
    const html = renderToStaticMarkup(
      <OverrideFieldRow {...base} state="no-default" value="" defaultText={undefined} />,
    )
    expect(html).toContain("No default")
    expect(html).toContain("Add")
  })

  it("overridden state: editable textarea + Reset + collapsible default", () => {
    const html = renderToStaticMarkup(
      <OverrideFieldRow {...base} state="overridden" value="Bye!" defaultText="Goodbye now." />,
    )
    expect(html).toContain("Overridden")
    expect(html).toContain("Bye!")
    expect(html).toContain("Reset to default")
    expect(html).toContain("Goodbye now.")
  })
})

describe("PreviewPane", () => {
  it("renders meta line and sections", () => {
    const html = renderToStaticMarkup(
      <PreviewPane
        title="Wrap Up"
        meta="v5 · pinned schema v2"
        loading={false}
        error={null}
        sections={[{ label: "Outro", text: "Goodbye now." }]}
      />,
    )
    expect(html).toContain("v5 · pinned schema v2")
    expect(html).toContain("Outro")
    expect(html).toContain("Goodbye now.")
  })
})

describe("VersionList", () => {
  const versions: PromptVersionSummary[] = [
    {
      id: "b",
      version: 2,
      status: "draft",
      created_at: "2026-07-09T10:00:00Z",
      schema_version_id: "s3",
      schema_version: 3,
    },
    {
      id: "a",
      version: 1,
      status: "published",
      created_at: "2026-07-08T10:00:00Z",
      schema_version_id: "s2",
      schema_version: 2,
    },
  ]

  it("shows version number, status badge, pinned schema, and actions", () => {
    const html = renderToStaticMarkup(
      <VersionList
        versions={versions}
        loadedVersionId="a"
        busy={false}
        publishingId={null}
        onLoad={() => undefined}
        onPublish={() => undefined}
      />,
    )
    expect(html).toContain("v2")
    expect(html).toContain("draft")
    expect(html).toContain("published")
    expect(html).toContain("pins schema v3")
    expect(html).toContain("Load")
    expect(html).toContain("Publish")
  })
})
