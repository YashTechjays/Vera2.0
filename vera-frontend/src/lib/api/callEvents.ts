// Live call-event SSE client (real-call flow). Envelope stream: transcript turns,
// call_status frames, and future event types (form-fill) ride one connection.
// fetch + ReadableStream (not EventSource) so the Authorization header can be sent;
// reconnect = re-call (the endpoint replays from the start). Mirrors transcription.ts.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type CallStreamEvent = { type: string; data: Record<string, unknown>; ts: number }
export type TranscriptTurn = { role: "user" | "agent"; text: string; ts: number }

/** Narrow an envelope to a transcript turn; null for other/malformed event types. */
export function asTranscriptTurn(e: CallStreamEvent): TranscriptTurn | null {
  if (e.type !== "transcript") return null
  const { role, text } = e.data as { role?: unknown; text?: unknown }
  if ((role !== "user" && role !== "agent") || typeof text !== "string") return null
  return { role, text, ts: e.ts }
}

export async function streamCallEvents(
  callId: string,
  opts: { signal: AbortSignal; onEvent: (e: CallStreamEvent) => void },
): Promise<void> {
  const res = await fetch(`${BASE_URL}/calls/${encodeURIComponent(callId)}/events`, {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
    signal: opts.signal,
  })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, null, `call event stream failed (${res.status})`)
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
      if (json) opts.onEvent(JSON.parse(json) as CallStreamEvent)
    }
  }
}
