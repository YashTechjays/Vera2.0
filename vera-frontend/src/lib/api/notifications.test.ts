import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import { asInterventionNeeded, streamNotifications, type AppNotification } from "@/lib/api/notifications"
import { ApiError } from "@/lib/api/errors"

describe("asInterventionNeeded", () => {
  it("narrows an intervention_needed notification", () => {
    const n: AppNotification = {
      id: "1-1",
      type: "intervention_needed",
      data: { call_id: "c-1", score: 30, flag: "conversation_loop", reason: "r" },
      ts: 1,
    }
    expect(asInterventionNeeded(n)).toEqual({
      callId: "c-1",
      score: 30,
      flag: "conversation_loop",
    })
  })

  it("returns null for other types or malformed data", () => {
    expect(asInterventionNeeded({ id: "1-2", type: "something_else", data: {}, ts: 1 })).toBeNull()
    expect(
      asInterventionNeeded({ id: "1-3", type: "intervention_needed", data: { score: 1 }, ts: 1 }),
    ).toBeNull()
  })
})

// One raw SSE frame: "id: <id>\ndata: <json>\n\n" — matches streamOnce's parser.
function frame(id: string, payload: Omit<AppNotification, "id">): string {
  return `id: ${id}\ndata: ${JSON.stringify(payload)}\n\n`
}

function sseResponse(rawFrames: string): Response {
  const enc = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(enc.encode(rawFrames))
      controller.close()
    },
  })
  return new Response(stream, { status: 200 })
}

describe("streamNotifications", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it("parses the id:/data: frame into a notification with the entry id attached", async () => {
    const payload = {
      type: "intervention_needed" as const,
      data: { call_id: "c-1", score: 40, flag: "off_script" },
      ts: 7,
    }
    const controller = new AbortController()
    const fetchMock = vi.fn().mockResolvedValueOnce(sseResponse(frame("42-0", payload)))
    vi.stubGlobal("fetch", fetchMock)

    const seen: AppNotification[] = []
    const done = streamNotifications({
      signal: controller.signal,
      onNotification: (n) => {
        seen.push(n)
        controller.abort() // one frame is enough — stop the infinite reconnect loop
      },
    })
    await done
    expect(seen).toEqual([{ id: "42-0", ...payload }])
  })

  it("reconnects after a premature clean close and keeps delivering notifications", async () => {
    vi.useFakeTimers()
    const first = frame("1-0", { type: "intervention_needed", data: { call_id: "c-1", score: 10, flag: "long_silence" }, ts: 1 })
    const second = frame("2-0", { type: "intervention_needed", data: { call_id: "c-2", score: 20, flag: "other" }, ts: 2 })
    // First connection dies cleanly (server idle-timeout close) with no abort;
    // the loop must reconnect on its own rather than treating this as terminal.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(sseResponse(first))
      .mockResolvedValueOnce(sseResponse(second))
    vi.stubGlobal("fetch", fetchMock)

    const controller = new AbortController()
    const seen: AppNotification[] = []
    const done = streamNotifications({
      signal: controller.signal,
      onNotification: (n) => {
        seen.push(n)
        if (seen.length === 2) controller.abort()
      },
    })
    await vi.advanceTimersByTimeAsync(1000) // ride out the reconnect backoff
    await done

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(seen.map((n) => n.id)).toEqual(["1-0", "2-0"])
  })

  it("recovers from a malformed frame instead of killing the stream", async () => {
    vi.useFakeTimers()
    // No `Accept`-worthy JSON on the data: line — streamOnce's JSON.parse throws,
    // which must surface as a retryable failure, not an uncaught rejection.
    const bad = sseResponse("id: 1-0\ndata: {not json\n\n")
    const good = frame("2-0", { type: "intervention_needed", data: { call_id: "c-1", score: 50, flag: "none" }, ts: 2 })
    const fetchMock = vi.fn().mockResolvedValueOnce(bad).mockResolvedValueOnce(sseResponse(good))
    vi.stubGlobal("fetch", fetchMock)

    const controller = new AbortController()
    const seen: AppNotification[] = []
    const done = streamNotifications({
      signal: controller.signal,
      onNotification: (n) => {
        seen.push(n)
        controller.abort()
      },
    })
    // The malformed frame counts as a failed attempt (consecutiveFailures: 0 -> 1),
    // so the backoff is 1000 * 2**1 = 2000ms, not the 1000ms a clean-close retry uses.
    await vi.advanceTimersByTimeAsync(2000) // ride out the reconnect backoff
    await done

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(seen.map((n) => n.id)).toEqual(["2-0"])
  })

  it("throws (and does not retry) a non-retryable request failure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 401 }))
    vi.stubGlobal("fetch", fetchMock)

    await expect(
      streamNotifications({ signal: new AbortController().signal, onNotification: () => {} }),
    ).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("returns promptly when aborted during the reconnect backoff", async () => {
    const payload = { type: "intervention_needed" as const, data: { call_id: "c-1", score: 1, flag: "other" }, ts: 1 }
    const fetchMock = vi.fn().mockResolvedValue(sseResponse(frame("1-0", payload)))
    vi.stubGlobal("fetch", fetchMock)

    const controller = new AbortController()
    const done = streamNotifications({
      signal: controller.signal,
      onNotification: () => controller.abort(), // abort as soon as the first frame lands
    })
    await done // must not hang in backoff
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
