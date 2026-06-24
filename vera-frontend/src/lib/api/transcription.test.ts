import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import { streamTranscription, type TranscriptEvent } from "./transcription"

function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f))
      controller.close()
    },
  })
}

afterEach(() => vi.unstubAllGlobals())

describe("streamTranscription", () => {
  it("parses SSE data frames into events in order", async () => {
    const frames = [
      'id: 1-0\ndata: {"role":"user","text":"hi","ts":1}\n\n',
      'id: 2-0\ndata: {"role":"agent","text":"hello","ts":2}\n\n',
    ]
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(sseStream(frames), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      ),
    )
    const seen: TranscriptEvent[] = []
    await streamTranscription("call--t--c", {
      signal: new AbortController().signal,
      onEvent: (e) => seen.push(e),
    })
    expect(seen).toEqual([
      { role: "user", text: "hi", ts: 1 },
      { role: "agent", text: "hello", ts: 2 },
    ])
  })

  it("sends the bearer token and hits the right url", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(sseStream([]), { status: 200 }))
    vi.stubGlobal("fetch", fetchMock)
    await streamTranscription("call--t--c", {
      signal: new AbortController().signal,
      onEvent: () => {},
    })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/voice-lab/sessions/call--t--c/transcript")
    expect(init.headers.Authorization).toBe("Bearer tok")
  })
})
