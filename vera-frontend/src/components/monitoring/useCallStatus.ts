import { useCallback, useState } from "react"

import { isTerminalCallStatus } from "@/lib/api/callEvents"

/**
 * Tracks a call's lifecycle from the call_status envelopes on the events stream:
 * when it started (the "active" event's ts — the instant the callee answered,
 * replayed to late joiners) and whether it reached a terminal status. Returns the
 * `onCallStatus` handler to pass to CallTranscript, plus `startedAtMs`, the
 * `ended` flag, and the terminal status itself (for status-specific banner copy —
 * busy vs no-answer vs canceled).
 *
 * The LiveKit room can outlive the call (a watching supervisor keeps it open), so
 * this — not room connection state — is the source of truth for "ended". The state
 * resets during render when the call changes (React's previous-render pattern; an
 * effect-based reset is forbidden by react-hooks v6's set-state-in-effect rule).
 */
export function useCallStatus(callId: string | undefined): {
  startedAtMs: number | null
  callEnded: boolean
  terminalStatus: string | null
  onCallStatus: (status: string, ts: number) => void
} {
  const [startedAtMs, setStartedAtMs] = useState<number | null>(null)
  const [terminalStatus, setTerminalStatus] = useState<string | null>(null)
  const [statusForCallId, setStatusForCallId] = useState(callId)
  if (callId !== statusForCallId) {
    setStatusForCallId(callId)
    setStartedAtMs(null)
    setTerminalStatus(null)
  }
  const onCallStatus = useCallback((status: string, ts: number) => {
    // A reconnect replays the stream, re-delivering "active" with the same ts —
    // re-setting the identical value is a no-op re-render-wise.
    if (status === "active") setStartedAtMs(ts)
    if (isTerminalCallStatus(status)) setTerminalStatus(status)
  }, [])
  return { startedAtMs, callEnded: terminalStatus !== null, terminalStatus, onCallStatus }
}
