import { describe, expect, it, vi } from "vitest"

// Importing callEvents.ts transitively pulls in auth/storage, which touches
// sessionStorage at module load — undefined in the node test env (mirrors
// transcription.test.ts's mocking approach).
vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import { asTranscriptTurn, type CallStreamEvent } from "@/lib/api/callEvents"

describe("asTranscriptTurn", () => {
  it("maps a transcript envelope to a turn", () => {
    const e: CallStreamEvent = {
      type: "transcript",
      data: { role: "agent", text: "hello" },
      ts: 42,
    }
    expect(asTranscriptTurn(e)).toEqual({ role: "agent", text: "hello", ts: 42 })
  })

  it("ignores non-transcript envelopes", () => {
    expect(asTranscriptTurn({ type: "call_status", data: { status: "active" }, ts: 1 })).toBeNull()
  })

  it("ignores malformed transcript data", () => {
    expect(asTranscriptTurn({ type: "transcript", data: { role: "narrator" }, ts: 1 })).toBeNull()
  })
})
