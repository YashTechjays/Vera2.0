import { afterEach, describe, expect, it, vi } from "vitest"

// Importing callEvents.ts transitively pulls in auth/storage, which touches
// sessionStorage at module load — undefined in the node test env (mirrors
// transcription.test.ts's mocking approach).
vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import {
  asCallStatus,
  asTranscriptTurn,
  isTerminalCallStatus,
  streamCallEvents,
  terminalStatusMessage,
  type CallStreamEvent,
} from "@/lib/api/callEvents"
import { ApiError } from "@/lib/api/errors"

describe("asTranscriptTurn", () => {
  it("maps a transcript envelope to a turn, keeping the acting source", () => {
    const e: CallStreamEvent = {
      type: "transcript",
      data: { role: "agent", source: "bot", text: "hello" },
      ts: 42,
    }
    expect(asTranscriptTurn(e)).toEqual({ role: "agent", source: "bot", text: "hello", ts: 42 })
  })

  it("maps a dtmf keypress envelope (bot action, non-speech role)", () => {
    const e: CallStreamEvent = {
      type: "transcript",
      data: { role: "dtmf", source: "bot", text: "3" },
      ts: 7,
    }
    expect(asTranscriptTurn(e)).toEqual({ role: "dtmf", source: "bot", text: "3", ts: 7 })
  })

  it("derives the source for a legacy envelope published before source existed", () => {
    expect(
      asTranscriptTurn({ type: "transcript", data: { role: "user", text: "hi" }, ts: 1 }),
    ).toEqual({ role: "user", source: "rep", text: "hi", ts: 1 })
    expect(
      asTranscriptTurn({ type: "transcript", data: { role: "agent", text: "yo" }, ts: 2 }),
    ).toEqual({ role: "agent", source: "bot", text: "yo", ts: 2 })
  })

  it("falls back to the role-derived source when the stamped source is unknown", () => {
    expect(
      asTranscriptTurn({
        type: "transcript",
        data: { role: "user", source: "martian", text: "hi" },
        ts: 1,
      }),
    ).toEqual({ role: "user", source: "rep", text: "hi", ts: 1 })
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

function sseResponse(events: CallStreamEvent[]): Response {
  const enc = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const e of events) controller.enqueue(enc.encode(`data: ${JSON.stringify(e)}\n\n`))
      controller.close()
    },
  })
  return new Response(stream, { status: 200 })
}

describe("streamCallEvents", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it("reconnects after a premature clean close and discards-and-replaces via onReconnect", async () => {
    vi.useFakeTimers()
    const turn: CallStreamEvent = { type: "transcript", data: { role: "agent", text: "hi" }, ts: 1 }
    const ended: CallStreamEvent = { type: "call_status", data: { status: "ended" }, ts: 2 }
    // First connection dies without a terminal status (LB idle timeout);
    // the second replays from the start and finishes the call.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(sseResponse([turn]))
      .mockResolvedValueOnce(sseResponse([turn, ended]))
    vi.stubGlobal("fetch", fetchMock)

    const seen: CallStreamEvent[] = []
    const onReconnect = vi.fn()
    const done = streamCallEvents("call-1", {
      signal: new AbortController().signal,
      onEvent: (e) => seen.push(e),
      onReconnect,
    })
    await vi.advanceTimersByTimeAsync(1000) // ride out the reconnect backoff
    await done // resolves — the second connection delivered a terminal status

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(onReconnect).toHaveBeenCalledTimes(1)
    expect(seen).toEqual([turn, turn, ended])
  })

  it("stops cleanly once a terminal status arrives (no further reconnect)", async () => {
    const ended: CallStreamEvent = { type: "call_status", data: { status: "completed" }, ts: 1 }
    const fetchMock = vi.fn().mockResolvedValue(sseResponse([ended]))
    vi.stubGlobal("fetch", fetchMock)

    await streamCallEvents("call-1", {
      signal: new AbortController().signal,
      onEvent: () => {},
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("throws (and does not retry) a non-retryable request failure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 404 }))
    vi.stubGlobal("fetch", fetchMock)

    await expect(
      streamCallEvents("call-1", { signal: new AbortController().signal, onEvent: () => {} }),
    ).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("returns promptly when aborted during the reconnect backoff", async () => {
    const turn: CallStreamEvent = { type: "transcript", data: { role: "agent", text: "hi" }, ts: 1 }
    const fetchMock = vi.fn().mockResolvedValue(sseResponse([turn]))
    vi.stubGlobal("fetch", fetchMock)

    const controller = new AbortController()
    const done = streamCallEvents("call-1", {
      signal: controller.signal,
      onEvent: () => controller.abort(), // abort as soon as the first frame lands
    })
    await done // must not hang in backoff
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe("terminalStatusMessage", () => {
  it("names the failure for a supervisor", () => {
    expect(terminalStatusMessage("busy")).toBe("Call failed — the line was busy.")
    expect(terminalStatusMessage("no_answer")).toBe("Call failed — no answer.")
    expect(terminalStatusMessage("failed")).toBe("Call failed.")
    expect(terminalStatusMessage("canceled")).toBe("Call canceled by a supervisor.")
  })

  it("reads as a normal ending otherwise", () => {
    expect(terminalStatusMessage("ended")).toBe("Call ended — no longer live.")
    expect(terminalStatusMessage("completed")).toBe("Call ended — no longer live.")
  })
})
