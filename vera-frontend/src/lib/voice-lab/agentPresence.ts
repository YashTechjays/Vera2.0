/** Utilities for detecting whether an AI agent worker has joined the LiveKit room. */

import { useEffect, useState } from "react"
import { ConnectionState } from "livekit-client"

/** Minimal interface — only the fields `hasAgentParticipant` needs from
 *  livekit-client's `Participant`. Kept narrow so unit tests can pass plain
 *  objects without importing the full livekit-client class. */
export interface ParticipantLike {
  isLocal: boolean
  isAgent: boolean
}

/** Returns true when at least one non-local, agent-kind participant is present.
 *  Uses `participant.isAgent` from livekit-client ≥ 1.x (backed by
 *  ParticipantKind.AGENT from @livekit/protocol), which is the most reliable
 *  signal — the server stamps the kind at publish time, so identity-prefix
 *  heuristics are not needed. The local participant is always the browser user,
 *  never the AI agent, so it is excluded even if `isAgent` is unexpectedly set.
 *
 *  Note: this detects the AI agent by LiveKit `ParticipantKind` (room-level),
 *  which is a different signal from the `source`/`role` fields on transcript
 *  events — those identify the speaker in a conversation turn, not the room
 *  participant kind. */
export function hasAgentParticipant(participants: ParticipantLike[]): boolean {
  return participants.some((p) => !p.isLocal && p.isAgent)
}

/** Returns `true` when the room has been Connected for longer than `timeoutMs`
 *  without an agent participant joining. Automatically clears (returns `false`)
 *  when `agentPresent` becomes true or when the room leaves the Connected state.
 *
 *  Arm/cancel logic: the timer is started in a `useEffect` that runs only when
 *  `state` changes to `Connected`. React's effect cleanup cancels the timer via
 *  `clearTimeout` before the next effect run, so no stale-callback guard is
 *  needed. `setState` is called only inside the async timer callback, never
 *  synchronously in the effect body, satisfying `react-hooks/set-state-in-effect`. */
export function useAgentJoinTimeout(
  state: ConnectionState,
  agentPresent: boolean,
  timeoutMs: number,
): boolean {
  const [timedOut, setTimedOut] = useState(false)

  useEffect(() => {
    if (state !== ConnectionState.Connected) return
    const timer = setTimeout(() => setTimedOut(true), timeoutMs)
    return () => {
      clearTimeout(timer)
      setTimedOut(false)
    }
  }, [state, timeoutMs])

  // Auto-clear if the agent joins after the timeout (late-join case).
  // agentPresent drives the derived value without an extra setState.
  return timedOut && state === ConnectionState.Connected && !agentPresent
}
