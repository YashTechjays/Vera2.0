import { describe, expect, it } from "vitest"

import { canApplyLiveAnswer } from "./liveAnswers"

const base = {
  loadedFormId: "form-1",
  expectedFormId: "form-1",
  path: "sections.patient.name",
  editedPaths: new Set<string>(),
}

describe("canApplyLiveAnswer", () => {
  it("applies an answer for the loaded form", () => {
    expect(canApplyLiveAnswer(base)).toBe(true)
  })

  it("drops the answer when no form is loaded", () => {
    // The live-filling regression: no form loaded, so every pushed answer was discarded.
    expect(canApplyLiveAnswer({ ...base, loadedFormId: null })).toBe(false)
  })

  it("drops the answer when the stream belongs to a different form", () => {
    expect(canApplyLiveAnswer({ ...base, expectedFormId: "form-2" })).toBe(false)
  })

  it("does not overwrite a field the supervisor edited this session", () => {
    expect(
      canApplyLiveAnswer({ ...base, editedPaths: new Set([base.path]) }),
    ).toBe(false)
  })

  it("still applies to other fields after an unrelated edit", () => {
    expect(
      canApplyLiveAnswer({ ...base, editedPaths: new Set(["sections.patient.dob"]) }),
    ).toBe(true)
  })
})
