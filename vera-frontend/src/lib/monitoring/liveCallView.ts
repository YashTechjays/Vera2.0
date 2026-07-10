// Pure view logic for the live-call panel, kept free of LiveKit imports so it
// unit-tests without a browser. Connection states arrive as livekit-client's
// ConnectionState string values.

export type ConnectionPhase = "connecting" | "live" | "reconnecting" | "ended"

export const CONNECTION_PHASE_LABEL: Record<ConnectionPhase, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  ended: "Call ended",
}

/** Map a raw connection state to a display phase. `everConnected` latches once a
 *  connection succeeds: before it, "disconnected" is just the auto-connect in
 *  flight; after it, a disconnect means the room is gone (call ended). */
export function connectionPhase(state: string, everConnected: boolean): ConnectionPhase {
  switch (state) {
    case "connected":
      return "live"
    case "reconnecting":
    case "signalReconnecting":
      return "reconnecting"
    default:
      return everConnected ? "ended" : "connecting"
  }
}

/** Connected but nobody else is in the room yet — the agent/callee are still joining. */
export function isWaitingForCall(state: string, remoteParticipantCount: number): boolean {
  return state === "connected" && remoteParticipantCount === 0
}

/** Tag for a participant row. Permissions can arrive a beat after the participant
 *  does — treat unknown as listen-only rather than overclaiming. */
export function participantModeLabel(canPublish: boolean | undefined): "Can speak" | "Listening" {
  return canPublish ? "Can speak" : "Listening"
}

export type SpeakerButtonState = {
  action: "unlock" | "mute" | "unmute"
  title: string
  /** Render the slashed (muted) icon variant. */
  slashed: boolean
}

/** One speaker button covers both jobs: unlocking autoplay-blocked audio and
 *  muting/unmuting output. The unlock action always wins — until the browser
 *  allows playback there is nothing to mute. */
export function speakerButtonState(canPlayAudio: boolean, outputMuted: boolean): SpeakerButtonState {
  if (!canPlayAudio) return { action: "unlock", title: "Enable audio", slashed: true }
  if (outputMuted) return { action: "unmute", title: "Unmute audio", slashed: true }
  return { action: "mute", title: "Mute audio", slashed: false }
}
