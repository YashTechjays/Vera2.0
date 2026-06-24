// Live transcript SSE client. Uses fetch + ReadableStream (not EventSource) so it can
// send the Authorization header; reconnect = re-call (the endpoint replays from the
// start). Parses text/event-stream frames and emits one event per finalized turn.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type TranscriptEvent = { role: "user" | "agent"; text: string; ts: number }

export async function streamTranscription(
  roomName: string,
  opts: { signal: AbortSignal; onEvent: (e: TranscriptEvent) => void },
): Promise<void> {
  const res = await fetch(
    `${BASE_URL}/voice-lab/sessions/${encodeURIComponent(roomName)}/transcript`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
      signal: opts.signal,
    },
  )
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, null, `transcript stream failed (${res.status})`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split("\n\n")
    buffer = frames.pop() ?? ""
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"))
      if (!dataLine) continue
      const json = dataLine.slice(5).trim()
      if (json) opts.onEvent(JSON.parse(json) as TranscriptEvent)
    }
  }
}
