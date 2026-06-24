import { describe, expect, it } from "vitest"

import { allowedStatusTransitions, statusActionLabel } from "./display"

describe("allowedStatusTransitions", () => {
  it("offers (re)queue + complete from exception_review", () => {
    expect(allowedStatusTransitions("exception_review")).toEqual(["in_queue", "completed"])
  })

  it("offers queue from ready_for_processing and call_failed", () => {
    expect(allowedStatusTransitions("ready_for_processing")).toEqual(["in_queue"])
    expect(allowedStatusTransitions("call_failed")).toEqual(["in_queue"])
  })

  it("offers nothing for pipeline-driven / terminal states", () => {
    for (const s of ["in_queue", "in_call", "ai_processing", "completed"] as const) {
      expect(allowedStatusTransitions(s)).toEqual([])
    }
  })
})

describe("statusActionLabel", () => {
  it("labels the complete and queue actions", () => {
    expect(statusActionLabel("completed")).toBe("Mark complete")
    expect(statusActionLabel("in_queue")).toBe("Send to queue")
  })
})
