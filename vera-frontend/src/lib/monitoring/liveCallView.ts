// Pure view logic for the live-call panel — no LiveKit imports, so it unit-tests without a browser.

export type ConnectionPhase = "connecting" | "live" | "reconnecting" | "ended"

export const CONNECTION_PHASE_LABEL: Record<ConnectionPhase, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  ended: "Call ended",
}

/** Map a raw connection state to a display phase; `everConnected` latches on first connect so a
 *  later disconnect reads as "ended", not "connecting". */
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

// Mirrors backend vocabulary (vera_core.observability.correlation): the vera.mode attr + supervisor-/monitor-/caller- identity prefixes.
export const MODE_ATTR = "vera.mode"
const HUMAN_IDENTITY_PREFIXES = ["supervisor-", "monitor-", "caller-"]
const SIP_CALLEE_IDENTITY = "phone-callee"

export type ParticipantMode = "intervener" | "listener" | "agent" | "callee"

/** The slice of a LiveKit Participant the view logic needs. `isAgent` is precomputed by the caller from `p.kind`. */
export type ParticipantLike = {
  identity: string
  name?: string
  isAgent?: boolean
  isLocal?: boolean
  attributes?: Readonly<Record<string, string>>
}

/** Room state the modal needs from inside LiveKit: call-over (gates closing),
 *  other-intervener (disables Intervene), and the intervener's label for the transcript. */
export type RoomStatus = {
  phase: ConnectionPhase
  otherIntervener: boolean
  intervenerLabel: string | null
}

export const PARTICIPANT_MODE_BADGE: Record<ParticipantMode, string> = {
  intervener: "Intervening",
  listener: "Listening",
  agent: "AI Agent",
  callee: "Caller",
}

/** Kind beats identity beats attribute; an unknown identity falls back to agent (self-hosted workers may lack ParticipantKind.Agent). */
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

/** The intervening supervisor's label (their email) for the transcript, or null. */
export function intervenerLabel(participants: ParticipantLike[]): string | null {
  const intervener = participants.find((p) => participantMode(p) === "intervener")
  return intervener ? participantLabel(intervener) : null
}

/** Connected but the AI agent hasn't entered the room yet. */
export function isWaitingForCall(state: string, participants: ParticipantLike[]): boolean {
  return state === "connected" && !agentJoined(participants)
}

export type LiveCallMode = "listen" | "intervene"

/** Radix routes every close path (X, Esc, overlay) here; an intervener can't leave until the call ends. */
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

/** Hidden without the permission; enabled only while live with the mic free — mirrors the backend calls:intervene gate + lock. */
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

/** One button for both jobs: unlock autoplay-blocked audio, then mute/unmute. Unlock wins — nothing to mute until playback is allowed. */
export function speakerButtonState(canPlayAudio: boolean, outputMuted: boolean): SpeakerButtonState {
  if (!canPlayAudio) return { action: "unlock", title: "Enable audio", slashed: true }
  if (outputMuted) return { action: "unmute", title: "Unmute audio", slashed: true }
  return { action: "mute", title: "Mute audio", slashed: false }
}
