import { useCallback, useState } from "react"

import { isTerminalCallStatus } from "@/lib/api/callEvents"

/**
 * Tracks whether the given call has reached a terminal status on the events stream.
 * Returns the `ended` flag, the terminal status itself (for status-specific banner
 * copy — busy vs no-answer vs canceled), and the `onCallStatus` handler to pass to
 * CallTranscript.
 *
 * The LiveKit room can outlive the call (a watching supervisor keeps it open), so
 * this — not room connection state — is the source of truth for "ended". The state
 * resets during render when the call changes (React's previous-render pattern; an
 * effect-based reset is forbidden by react-hooks v6's set-state-in-effect rule).
 */
export function useCallEnded(callId: string | undefined): {
  callEnded: boolean
  terminalStatus: string | null
  onCallStatus: (status: string) => void
} {
  const [terminalStatus, setTerminalStatus] = useState<string | null>(null)
  const [endedForCallId, setEndedForCallId] = useState(callId)
  if (callId !== endedForCallId) {
    setEndedForCallId(callId)
    setTerminalStatus(null)
  }
  const onCallStatus = useCallback((status: string) => {
    if (isTerminalCallStatus(status)) setTerminalStatus(status)
  }, [])
  return { callEnded: terminalStatus !== null, terminalStatus, onCallStatus }
}
