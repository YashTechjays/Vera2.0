// Interpreting the LiveKit room-metadata "call_failed" signal the control plane sets
// when an outbound call fails. The backend event carries only a reason code; the UI
// owns the human-facing copy (kept here so it is unit-testable without React).

export type CallFailureReason = "no_answer" | "busy_or_declined" | "failed"

const MESSAGES: Record<CallFailureReason, string> = {
  no_answer: "The call wasn't answered — it rang but nobody picked up.",
  busy_or_declined: "The call was declined or the line was busy.",
  failed: "The call couldn't be completed. Check the number and try again.",
}

const GENERIC = "The call could not be completed."

export function callFailureMessage(reason: CallFailureReason): string {
  return MESSAGES[reason]
}

/** Parse LiveKit room metadata (a JSON string) for a call-failure signal. Returns the
 *  user-facing message, or null if metadata is absent, unparseable, or not a
 *  call_failed status. An unknown reason yields a generic (non-null) message. */
export function parseCallFailure(metadata: string | undefined): string | null {
  if (!metadata) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(metadata)
  } catch {
    return null
  }
  if (typeof parsed !== "object" || parsed === null) return null
  const record = parsed as { status?: unknown; reason?: unknown }
  if (record.status !== "call_failed") return null
  if (record.reason === "no_answer" || record.reason === "busy_or_declined" || record.reason === "failed") {
    return callFailureMessage(record.reason)
  }
  return GENERIC
}
