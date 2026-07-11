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

// Mirrors the backend vocabulary (vera_core.observability.correlation): the
// control plane stamps each supervisor token with vera.mode, and human
// participant identities are prefixed supervisor-/monitor-/caller-.
export const MODE_ATTR = "vera.mode"
const HUMAN_IDENTITY_PREFIXES = ["supervisor-", "monitor-", "caller-"]
const SIP_CALLEE_IDENTITY = "phone-callee"

export type ParticipantMode = "intervener" | "listener" | "agent" | "callee"

/** The slice of a LiveKit Participant the view logic needs — kept structural so
 *  it unit-tests without a browser. `isAgent` is `p.kind === ParticipantKind.Agent`,
 *  precomputed by the caller. */
export type ParticipantLike = {
  identity: string
  name?: string
  isAgent?: boolean
  isLocal?: boolean
  attributes?: Readonly<Record<string, string>>
}

/** Room state the modal needs from inside the LiveKit context: whether the
 *  call is over (gates closing while intervening) and whether someone else
 *  holds the mic (disables Intervene live). */
export type RoomStatus = {
  phase: ConnectionPhase
  otherIntervener: boolean
}

export const PARTICIPANT_MODE_BADGE: Record<ParticipantMode, string> = {
  intervener: "Intervening",
  listener: "Listening",
  agent: "AI Agent",
  callee: "Caller",
}

/** Kind beats identity beats attribute; an unrecognized identity is treated as
 *  the agent (self-hosted workers may not carry ParticipantKind.Agent). */
export function participantMode(p: ParticipantLike): ParticipantMode {
  if (p.isAgent) return "agent"
  if (p.identity === SIP_CALLEE_IDENTITY) return "callee"
  const mode = p.attributes?.[MODE_ATTR]
  if (mode === "intervener" || mode === "listener") return mode
  if (HUMAN_IDENTITY_PREFIXES.some((prefix) => p.identity.startsWith(prefix))) return "listener"
  return "agent"
}

/** Display name: supervisors carry their email as the token name. */
export function participantLabel(p: ParticipantLike): string {
  const mode = participantMode(p)
  if (mode === "agent") return "Vera Agent"
  if (mode === "callee") return "Caller"
  return p.name || p.identity
}

export function agentJoined(participants: ParticipantLike[]): boolean {
  return participants.some((p) => participantMode(p) === "agent")
}

/** Someone ELSE holds the mic — the local user's Intervene action must disable. */
export function otherIntervenerPresent(participants: ParticipantLike[]): boolean {
  return participants.some((p) => !p.isLocal && participantMode(p) === "intervener")
}

/** Connected but the AI agent hasn't entered the room yet. */
export function isWaitingForCall(state: string, participants: ParticipantLike[]): boolean {
  return state === "connected" && !agentJoined(participants)
}

export type LiveCallMode = "listen" | "intervene"

/** Radix routes every close path (X, Esc, overlay) through onOpenChange —
 *  an intervener may not leave until the call is over (End Call or the room
 *  dying on its own). */
export function shouldAllowClose(
  mode: LiveCallMode,
  callEnded: boolean,
  requestedOpen: boolean,
): boolean {
  if (requestedOpen) return true
  return mode !== "intervene" || callEnded
}

export type InterveneButtonState = {
  visible: boolean
  disabled: boolean
  title?: string
}

/** Intervene is hidden without the permission, and enabled only while live
 *  with the mic free — mirroring the backend's calls:intervene gate + lock. */
export function interveneButtonState(
  canIntervene: boolean,
  status: RoomStatus | null,
): InterveneButtonState {
  if (!canIntervene) return { visible: false, disabled: true }
  if (status?.phase !== "live") return { visible: true, disabled: true }
  if (status.otherIntervener) {
    return { visible: true, disabled: true, title: "Another supervisor is intervening" }
  }
  return { visible: true, disabled: false }
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
