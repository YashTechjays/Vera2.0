import { describe, expect, it } from "vitest"

import { parseCallFailure } from "./callFailure"

describe("parseCallFailure", () => {
  it("returns null for absent metadata", () => {
    expect(parseCallFailure(undefined)).toBeNull()
    expect(parseCallFailure("")).toBeNull()
  })

  it("returns null for unparseable metadata", () => {
    expect(parseCallFailure("{not json")).toBeNull()
  })

  it("returns null when status is not call_failed", () => {
    expect(parseCallFailure(JSON.stringify({ status: "active" }))).toBeNull()
  })

  it("maps each known reason to its message", () => {
    expect(parseCallFailure(JSON.stringify({ status: "call_failed", reason: "no_answer" }))).toMatch(
      /wasn't answered/i,
    )
    expect(
      parseCallFailure(JSON.stringify({ status: "call_failed", reason: "busy_or_declined" })),
    ).toMatch(/declined or the line was busy/i)
    expect(parseCallFailure(JSON.stringify({ status: "call_failed", reason: "failed" }))).toMatch(
      /couldn't be completed/i,
    )
  })

  it("falls back to a generic message for an unknown reason", () => {
    expect(parseCallFailure(JSON.stringify({ status: "call_failed", reason: "??" }))).toMatch(
      /could not be completed/i,
    )
  })
})
