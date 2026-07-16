import { describe, expect, it } from "vitest"

import type { PromptDocument } from "@/lib/api/prompts"
import {
  clearOverrideField,
  clientValidationErrors,
  documentsEqual,
  hasErrorsFor,
  insertToken,
  normalizeDocument,
  overrideStateOf,
  parsePromptErrorList,
  parsePromptErrors,
  placeholderGroupsOf,
  removeOverrideEntry,
  setOverrideField,
  taskDefaultsOf,
} from "./document"

function doc(overrides: PromptDocument["task_overrides"] = {}): PromptDocument {
  return {
    kind: "prompt_document",
    session: { persona: "p", goal: "g", base_instructions: "b" },
    task_overrides: overrides,
  }
}

// Shaped like the raw schema_version document (GET /prompts/{id}/schema `document`).
const rawSchemaDoc = {
  dsl_version: "2.1",
  name: "IBV",
  insurance_type: "infertility_treatment",
  system_fields: { member_id: "sections.basics.plan_type", patient_name: "sections.info.name" },
  sections: {
    basics: {
      title: "Basics",
      fields: {
        plan_type: { type: "text", title: "Plan Type", role: "ask", prompt: { ask: "?" } },
        meta: {
          type: "group",
          title: "Meta",
          fields: { bg: { type: "text", title: "Background", role: "context" } },
        },
      },
    },
    info: {
      title: "Info",
      fields: { name: { type: "text", title: "Name", role: "context" } },
    },
  },
  tasks: [
    { task_key: "main", title: "Main", intro: "Hello.", prompt: "Do the thing.", sections: ["basics"] },
    { task_key: "wrap_up", title: "Wrap Up", outro: null, sections: [] },
  ],
}

describe("override ops", () => {
  it("setOverrideField creates the entry and field immutably", () => {
    const d = doc()
    const next = setOverrideField(d, "wrap_up", "outro", "bye")
    expect(next.task_overrides).toEqual({ wrap_up: { outro: "bye" } })
    expect(d.task_overrides).toEqual({})
  })

  it("clearOverrideField drops the field, and the entry when it was the last field", () => {
    const d = doc({ wrap_up: { outro: "bye", intro: "hi" } })
    const one = clearOverrideField(d, "wrap_up", "intro")
    expect(one.task_overrides).toEqual({ wrap_up: { outro: "bye" } })
    const none = clearOverrideField(one, "wrap_up", "outro")
    expect(none.task_overrides).toEqual({})
  })

  it("removeOverrideEntry drops a whole entry (orphaned-override cleanup)", () => {
    const d = doc({ ghost: { prompt: "x" }, wrap_up: { outro: "bye" } })
    expect(removeOverrideEntry(d, "ghost").task_overrides).toEqual({ wrap_up: { outro: "bye" } })
  })

  it("overrideStateOf distinguishes overridden / default / no-default", () => {
    const d = doc({ main: { intro: "custom" } })
    expect(overrideStateOf(d, "main", "intro", "Hello.")).toBe("overridden")
    expect(overrideStateOf(d, "main", "prompt", "Do the thing.")).toBe("default")
    expect(overrideStateOf(d, "wrap_up", "outro", undefined)).toBe("no-default")
  })
})

describe("normalize + equality", () => {
  it("normalizeDocument strips null fields and empty entries (server nulls)", () => {
    const server = doc({ wrap_up: { intro: null, outro: "bye", prompt: null }, empty: { intro: null } })
    expect(normalizeDocument(server).task_overrides).toEqual({ wrap_up: { outro: "bye" } })
  })

  it("documentsEqual ignores null-vs-absent and key order", () => {
    const a = doc({ wrap_up: { outro: "bye", intro: null }, main: { prompt: "x" } })
    const b = doc({ main: { prompt: "x" }, wrap_up: { outro: "bye" } })
    expect(documentsEqual(a, b)).toBe(true)
    expect(documentsEqual(a, doc({ wrap_up: { outro: "bye!" }, main: { prompt: "x" } }))).toBe(false)
  })
})

describe("validation", () => {
  it("clientValidationErrors flags empty session fields and empty overrides", () => {
    const d: PromptDocument = {
      kind: "prompt_document",
      session: { persona: "", goal: "g", base_instructions: "b" },
      task_overrides: { main: { intro: "  " } },
    }
    const errors = clientValidationErrors(d)
    expect(errors["session.persona"]).toEqual(["Required."])
    expect(errors["task_overrides.main.intro"]).toEqual([
      "An override cannot be empty — use Reset to remove it.",
    ])
    expect(Object.keys(clientValidationErrors(doc())).length).toBe(0)
  })

  it("hasErrorsFor matches by field-error key prefix (rail error dots)", () => {
    const fieldErrors = {
      "session.persona": ["Required."],
      "task_overrides.wrap_up.outro": ["unknown placeholder {{a}}"],
    }
    expect(hasErrorsFor(fieldErrors, "session.")).toBe(true)
    expect(hasErrorsFor(fieldErrors, "task_overrides.wrap_up.")).toBe(true)
    expect(hasErrorsFor(fieldErrors, "task_overrides.main.")).toBe(false)
    expect(hasErrorsFor({}, "session.")).toBe(false)
  })

  it("parsePromptErrors maps location-prefixed messages onto fields", () => {
    const parsed = parsePromptErrors(
      "session.persona: unknown placeholder {{patietn}}; " +
        "task_overrides.wrap_up.outro: unknown placeholder {{a}}; " +
        "task_overrides.wrap_up.outro: unknown placeholder {{b}}; " +
        "task_overrides.ghost: unknown task_key",
    )
    expect(parsed.fields["session.persona"]).toEqual(["unknown placeholder {{patietn}}"])
    expect(parsed.fields["task_overrides.wrap_up.outro"]).toEqual([
      "unknown placeholder {{a}}",
      "unknown placeholder {{b}}",
    ])
    expect(parsed.general).toEqual(["task_overrides.ghost: unknown task_key"])
  })

  it("parsePromptErrorList keeps a message containing '; ' intact (no re-split)", () => {
    const parsed = parsePromptErrorList([
      'session.goal: unknown placeholder {{oops; typo}}',
      "task_overrides.wrap_up.outro: unknown placeholder {{a}}",
    ])
    expect(parsed.fields["session.goal"]).toEqual(["unknown placeholder {{oops; typo}}"])
    expect(parsed.fields["task_overrides.wrap_up.outro"]).toEqual(["unknown placeholder {{a}}"])
    expect(parsed.general).toEqual([])
  })
})

describe("schema extraction", () => {
  it("taskDefaultsOf lists tasks with null text normalized to undefined", () => {
    expect(taskDefaultsOf(rawSchemaDoc)).toEqual([
      { task_key: "main", title: "Main", intro: "Hello.", outro: undefined, prompt: "Do the thing." },
      { task_key: "wrap_up", title: "Wrap Up", intro: undefined, outro: undefined, prompt: undefined },
    ])
  })

  it("placeholderGroupsOf collects system_fields keys and context leaf paths (nested groups)", () => {
    const groups = placeholderGroupsOf(rawSchemaDoc)
    expect(groups.system).toEqual([
      { token: "member_id", detail: "sections.basics.plan_type" },
      { token: "patient_name", detail: "sections.info.name" },
    ])
    expect(groups.context).toEqual([
      { token: "sections.basics.meta.bg", detail: "Background" },
      { token: "sections.info.name", detail: "Name" },
    ])
  })

  it("extraction tolerates a malformed document", () => {
    expect(taskDefaultsOf(null)).toEqual([])
    expect(placeholderGroupsOf({ sections: 7 })).toEqual({ system: [], context: [] })
  })
})

describe("insertToken", () => {
  it("inserts {{token}} at the caret and reports the new caret", () => {
    expect(insertToken("Hello world", "member_id", 6)).toEqual({
      next: "Hello {{member_id}}world",
      caret: 19,
    })
  })

  it("appends when the caret is unknown", () => {
    expect(insertToken("Hi", "member_id", null)).toEqual({ next: "Hi{{member_id}}", caret: 15 })
  })
})
