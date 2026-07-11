import { describe, expect, it, vi } from "vitest"

// Importing callEvents.ts transitively pulls in auth/storage, which touches
// sessionStorage at module load — undefined in the node test env (mirrors
// transcription.test.ts's mocking approach).
vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import {
  asCallStatus,
  asTranscriptTurn,
  isTerminalCallStatus,
  type CallStreamEvent,
} from "@/lib/api/callEvents"

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

describe("asCallStatus", () => {
  it("maps a call_status envelope to its status", () => {
    expect(asCallStatus({ type: "call_status", data: { status: "ended" }, ts: 1 })).toBe("ended")
  })

  it("ignores non-status envelopes", () => {
    expect(asCallStatus({ type: "transcript", data: { role: "agent", text: "x" }, ts: 1 })).toBeNull()
  })

  it("ignores malformed status data", () => {
    expect(asCallStatus({ type: "call_status", data: { status: 7 }, ts: 1 })).toBeNull()
  })
})

describe("isTerminalCallStatus", () => {
  it.each(["ended", "completed", "failed", "no_answer", "busy", "canceled"])(
    "%s is terminal",
    (s) => {
      expect(isTerminalCallStatus(s)).toBe(true)
    },
  )

  it.each(["active", "ringing", "ivr", "waiting", "critical", "initiated"])(
    "%s is not terminal",
    (s) => {
      expect(isTerminalCallStatus(s)).toBe(false)
    },
  )
})
