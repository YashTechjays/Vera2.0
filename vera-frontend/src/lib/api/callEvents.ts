// Live call-event SSE client (real-call flow). Envelope stream: transcript turns,
// call_status frames, and future event types (form-fill) ride one connection.
// fetch + ReadableStream (not EventSource) so the Authorization header can be sent.
// Reconnects itself on premature closes (LB idle timeout, backend redeploy): the
// endpoint replays from the start, so the caller's onReconnect discards stale
// state and the replay replaces it. Mirrors transcription.ts.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type CallStreamEvent = { type: string; data: Record<string, unknown>; ts: number }

/** Who acted (the constrained actor set — drives which side a turn renders on).
 *  "supervisor" is a human who took over the call (transcribed post-intervene). */
export type TranscriptTurnSource = "rep" | "bot" | "supervisor"
/** What kind of turn it was: "dtmf" = a keypad press whose text is the digits
 *  sent; "coaching"/"whisper" = a supervisor note to Vera (typed vs. spoken),
 *  never heard by anyone on the call. */
export type TranscriptTurnRole = "user" | "agent" | "dtmf" | "coaching" | "whisper"
export type TranscriptTurn = {
  role: TranscriptTurnRole
  source: TranscriptTurnSource
  text: string
  ts: number
  /** The specific supervisor who spoke/coached this line, if known — absent for
   *  Vera, the rep, and any turn published before speaker attribution existed. */
  speakerUserId?: string
}

// Fallback for envelopes published before `source` existed (or with a corrupted one).
const SOURCE_BY_ROLE: Record<TranscriptTurnRole, TranscriptTurnSource> = {
  user: "rep",
  agent: "bot",
  dtmf: "bot",
  coaching: "supervisor",
  whisper: "supervisor",
}

function isTurnRole(role: unknown): role is TranscriptTurnRole {
  return (
    role === "user" ||
    role === "agent" ||
    role === "dtmf" ||
    role === "coaching" ||
    role === "whisper"
  )
}

function isTurnSource(source: unknown): source is TranscriptTurnSource {
  return source === "rep" || source === "bot" || source === "supervisor"
}

/** Narrow an envelope to a transcript turn; null for other/malformed event types. */
export function asTranscriptTurn(e: CallStreamEvent): TranscriptTurn | null {
  if (e.type !== "transcript") return null
  const { role, source, text, user_id } = e.data as {
    role?: unknown
    source?: unknown
    text?: unknown
    user_id?: unknown
  }
  if (!isTurnRole(role) || typeof text !== "string") return null
  return {
    role,
    source: isTurnSource(source) ? source : SOURCE_BY_ROLE[role],
    text,
    ts: e.ts,
    speakerUserId: typeof user_id === "string" ? user_id : undefined,
  }
}

/** Narrow an envelope to a call-status value; null for other/malformed event types. */
export function asCallStatus(e: CallStreamEvent): string | null {
  if (e.type !== "call_status") return null
  const { status } = e.data as { status?: unknown }
  return typeof status === "string" ? status : null
}

/** One call-health-observer assessment (the "health" envelope). */
export type CallHealth = {
  /** 0-100; higher is healthier. */
  score: number
  /** "none" (healthy) or an intervention category (conversation_loop, ...). */
  flag: string
  /** LLM's one-line justification (PHI — session-scoped state only). */
  reason: string | null
  ts: number
}

/** Narrow an envelope to a health assessment; null for other/malformed types. */
export function asCallHealth(e: CallStreamEvent): CallHealth | null {
  if (e.type !== "health") return null
  const { score, flag, reason } = e.data as { score?: unknown; flag?: unknown; reason?: unknown }
  if (typeof score !== "number" || typeof flag !== "string") return null
  return { score, flag, reason: typeof reason === "string" ? reason : null, ts: e.ts }
}

// The worker publishes "ended" on its live stream; the DB replay of an already-terminal
// call carries the CallStatus enum value instead — treat both vocabularies as terminal.
const TERMINAL_CALL_STATUSES = new Set([
  "ended",
  "completed",
  "failed",
  "no_answer",
  "busy",
  "canceled",
])

/** Whether a call_status value means the call is over (no longer live). */
export function isTerminalCallStatus(status: string): boolean {
  return TERMINAL_CALL_STATUSES.has(status)
}

/** Supervisor-facing banner line for a terminal call status. Failure statuses
 *  name what happened (busy / no answer); anything else reads as a normal end. */
export function terminalStatusMessage(status: string | null): string {
  switch (status) {
    case "busy":
      return "Call failed — the line was busy."
    case "no_answer":
      return "Call failed — no answer."
    case "failed":
      return "Call failed."
    case "canceled":
      return "Call canceled by a supervisor."
    default:
      return "Call ended — no longer live."
  }
}

/**
 * Stream a call's events until the call reaches a terminal status or `signal`
 * aborts. A stream that dies early — network failure, 5xx, or a clean close
 * with no terminal status seen (LB idle timeout, backend redeploy) — is
 * reconnected with capped backoff. The endpoint replays from the start on
 * every connection, so before the replacement events arrive `onReconnect`
 * fires and the caller must discard everything received so far. Non-retryable
 * request failures (4xx: session expired, permission revoked, call hidden)
 * throw an ApiError.
 */
export async function streamCallEvents(
  callId: string,
  opts: {
    signal: AbortSignal
    onEvent: (e: CallStreamEvent) => void
    /** The stream is being replayed from the start — discard prior events. */
    onReconnect?: () => void
  },
): Promise<void> {
  let reconnecting = false
  let consecutiveFailures = 0
  for (;;) {
    // Discard-and-replace on the replacement's FIRST event, not at connect
    // time — a failed reconnect attempt then never blanks a visible transcript.
    let needsReset = reconnecting
    let sawTerminal = false
    const forward = (e: CallStreamEvent) => {
      if (needsReset) {
        needsReset = false
        opts.onReconnect?.()
      }
      const status = asCallStatus(e)
      if (status && isTerminalCallStatus(status)) sawTerminal = true
      opts.onEvent(e)
    }
    try {
      await streamOnce(callId, opts.signal, forward)
      consecutiveFailures = 0
    } catch (err) {
      if (opts.signal.aborted) return
      if (err instanceof ApiError && err.httpStatus < 500) throw err
      consecutiveFailures += 1
    }
    if (opts.signal.aborted || sawTerminal) return
    reconnecting = true
    await backoffDelay(Math.min(1000 * 2 ** consecutiveFailures, 15_000), opts.signal)
  }
}

/** One SSE connection: resolves on a clean close, throws on request/read failure. */
async function streamOnce(
  callId: string,
  signal: AbortSignal,
  onEvent: (e: CallStreamEvent) => void,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/calls/${encodeURIComponent(callId)}/events`, {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
    signal,
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
      if (json) onEvent(JSON.parse(json) as CallStreamEvent)
    }
  }
}

/** Abortable pause between reconnect attempts — resolves early (no error) on abort. */
function backoffDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      signal.removeEventListener("abort", finish)
      resolve()
    }
    const timer = setTimeout(finish, ms)
    signal.addEventListener("abort", finish)
  })
}
