import { useEffect, useState } from "react"
import { ChevronDown, Loader2, Mic, MicOff, PhoneOff, Radio, Volume2, VolumeX } from "lucide-react"
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useAudioPlayback,
  useConnectionState,
  useParticipantAttributes,
  useParticipants,
  useTrackToggle,
} from "@livekit/components-react"
import {
  ConnectionState,
  DisconnectReason,
  ParticipantKind,
  Track,
  type Participant,
} from "livekit-client"

import { Button } from "@/components/ui/button"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { ApiError } from "@/lib/api/client"
import { getJoinToken, type JoinTokenResponse } from "@/lib/api/calls"
import { terminalStatusMessage } from "@/lib/api/callEvents"
import { LIVE_CALL_ACTIVITY_EVENT, LIVE_CALL_ACTIVITY_INTERVAL_MS } from "@/lib/auth/idle"
import {
  CONNECTION_PHASE_LABEL,
  PARTICIPANT_MODE_BADGE,
  connectionPhase,
  isWaitingForCall,
  intervenerLabel,
  otherIntervenerPresent,
  participantLabel,
  participantMode,
  rosterVisible,
  speakerButtonState,
  type ConnectionPhase,
  type LiveCallMode,
  type ParticipantLike,
  type ParticipantMode,
  type RoomStatus,
} from "@/lib/monitoring/liveCallView"

const PHASE_ICON_CLASS: Record<ConnectionPhase, string> = {
  connecting: "text-muted-foreground",
  live: "text-emerald-600",
  reconnecting: "text-amber-600",
  ended: "text-muted-foreground",
  replaced: "text-amber-600",
}

const MODE_BADGE_CLASS: Record<ParticipantMode, string> = {
  intervener: "text-amber-600",
  listener: "text-muted-foreground",
  agent: "text-emerald-600",
  callee: "text-muted-foreground",
}

// Keeps the session alive while connected — a passive listener sends no input, so without this it
// idle-expires and logs the supervisor out mid-call. Must render inside <LiveKitRoom>.
function LiveActivityBeacon() {
  const state = useConnectionState()
  useEffect(() => {
    if (state !== ConnectionState.Connected) return
    const beat = () => window.dispatchEvent(new Event(LIVE_CALL_ACTIVITY_EVENT))
    beat()
    const id = window.setInterval(beat, LIVE_CALL_ACTIVITY_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [state])
  return null
}

function toParticipantLike(p: Participant): ParticipantLike {
  return {
    identity: p.identity,
    name: p.name,
    isAgent: p.kind === ParticipantKind.AGENT,
    isLocal: p.isLocal,
    attributes: p.attributes,
  }
}

function ParticipantRow({ participant }: { participant: Participant }) {
  // Subscribes to attributesChanged so a mode flip re-renders this row live.
  const { attributes } = useParticipantAttributes({ participant })
  const like = { ...toParticipantLike(participant), attributes }
  const mode = participantMode(like)
  return (
    <li className="flex items-center justify-between gap-2">
      <span className="truncate text-xs">
        {participantLabel(like)}
        {participant.isLocal && <span className="text-muted-foreground"> (you)</span>}
      </span>
      <span className={`shrink-0 text-xs ${MODE_BADGE_CLASS[mode]}`}>
        {PARTICIPANT_MODE_BADGE[mode]}
      </span>
    </li>
  )
}

/** One button, two jobs: unlock autoplay-blocked audio, then mute/unmute output. */
function SpeakerToggle({
  outputMuted,
  onOutputMutedChange,
}: {
  outputMuted: boolean
  onOutputMutedChange: (muted: boolean) => void
}) {
  const { canPlayAudio, startAudio } = useAudioPlayback()
  const speaker = speakerButtonState(canPlayAudio, outputMuted)
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      title={speaker.title}
      aria-label={speaker.title}
      onClick={() => {
        if (speaker.action === "unlock") void startAudio()
        else onOutputMutedChange(speaker.action === "mute")
      }}
    >
      {speaker.slashed ? <VolumeX className="size-4" /> : <Volume2 className="size-4" />}
    </Button>
  )
}

function micToggleTitle(canSpeak: boolean, enabled: boolean): string {
  if (!canSpeak) return "Listen-only — intervene to speak"
  return enabled ? "Mute microphone" : "Unmute microphone"
}

/** Mic mute/unmute; disabled while listen-only (the server, not this button, keeps listeners mute). */
function MicToggle({ canSpeak }: { canSpeak: boolean }) {
  const { enabled, pending, toggle } = useTrackToggle({ source: Track.Source.Microphone })
  const title = micToggleTitle(canSpeak, enabled)
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      title={title}
      aria-label={title}
      disabled={!canSpeak || pending}
      onClick={() => void toggle()}
    >
      {canSpeak && enabled ? <Mic className="size-4" /> : <MicOff className="size-4" />}
    </Button>
  )
}

/** This window is off the call but the call isn't over. `action` is offered only when
 *  rejoining can't take the seat from another window of ours — see the replaced case. */
function RoomNotice({
  message,
  action,
  onAction,
}: {
  message: string
  action?: string
  onAction?: () => void
}) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-700">
      <span>{message}</span>
      {action && onAction && (
        <button onClick={onAction} className="font-medium underline">
          {action}
        </button>
      )}
    </div>
  )
}

function RoomView({
  microphone,
  onStatus,
  ended,
  onReconnect,
  replaced,
}: {
  microphone: boolean
  onStatus?: (status: RoomStatus) => void
  ended: boolean
  onReconnect: () => void
  replaced: boolean
}) {
  const state = useConnectionState()
  const participants = useParticipants()
  const [outputMuted, setOutputMuted] = useState(false)
  const [rosterOpen, setRosterOpen] = useState(true)
  // Latch: once connected, a later disconnect means the room is gone, not a connect still in flight.
  const [everConnected, setEverConnected] = useState(false)
  if (state === ConnectionState.Connected && !everConnected) setEverConnected(true)

  const likes = participants.map(toParticipantLike)
  const phase = connectionPhase(state, everConnected, replaced)
  const otherIntervener = otherIntervenerPresent(likes)
  const supervisorLabel = intervenerLabel(likes)
  const takeoverLive = microphone || otherIntervener
  const roster = participants.filter((p) => rosterVisible(toParticipantLike(p), takeoverLive))
  // Our connection dropped, but the call itself isn't over (SSE, via `ended`) — offer a rejoin.
  const connectionLost = phase === "ended" && !ended

  useEffect(() => {
    onStatus?.({ phase, otherIntervener, intervenerLabel: supervisorLabel })
  }, [onStatus, phase, otherIntervener, supervisorLabel])

  return (
    <div className="flex flex-col gap-3 p-4 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Radio className={`size-4 ${PHASE_ICON_CLASS[phase]}`} />
          <span className="font-medium">{CONNECTION_PHASE_LABEL[phase]}</span>
        </div>
        <div className="flex items-center gap-1">
          <SpeakerToggle outputMuted={outputMuted} onOutputMutedChange={setOutputMuted} />
          <MicToggle canSpeak={microphone} />
        </div>
      </div>
      {connectionLost && (
        <RoomNotice
          message="Connection lost — the call may still be live."
          action="Reconnect"
          onAction={onReconnect}
        />
      )}
      {/* No rejoin here: rejoining reclaims the identity, evicting the other tab, which
          is then offered the same button — the seat ping-pongs. Send them there instead. */}
      {phase === "replaced" && (
        <RoomNotice message="You opened this call in another tab — continue there." />
      )}
      {isWaitingForCall(state, likes) && (
        <p className="text-muted-foreground">Waiting for the call…</p>
      )}
      {roster.length > 0 && (
        <Collapsible open={rosterOpen} onOpenChange={setRosterOpen}>
          <CollapsibleTrigger
            className="flex w-full items-center justify-between"
            title={rosterOpen ? "Collapse participants" : "Expand participants"}
          >
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Participants ({roster.length})
            </span>
            <ChevronDown
              className={`size-3.5 text-muted-foreground transition-transform ${rosterOpen ? "" : "-rotate-90"}`}
            />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ul className="mt-1 space-y-1">
              {roster.map((p) => (
                <ParticipantRow key={p.sid} participant={p} />
              ))}
            </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
      <RoomAudioRenderer muted={outputMuted} />
    </div>
  )
}

/**
 * Joins a call's LiveKit room via a server-minted token and renders the live panel.
 *
 * Changing `mode` needs a new token, and LiveKit ignores a token swap while
 * connected — the parent must remount (key on the mode) to switch.
 */
export function LiveCallRoom({
  callId,
  mode = "listen",
  ended = false,
  endedStatus = null,
  onStatus,
  onJoinFailed,
}: {
  callId: string
  /** Publish the local mic. "intervene" = a supervisor speaking over the agent; "callee" = the
   *  browser standing in for the payer rep (test transport). A viewer must never be audible,
   *  and getUserMedia may be blocked (e.g. incognito). */
  mode?: LiveCallMode
  /** Call hit a terminal status (events stream). The room can outlive the call while a supervisor
   *  sits in it, so room state alone would keep reading "Live" after the callee hung up. */
  ended?: boolean
  /** The terminal status, for status-specific banner copy (a failed dial reads "Call failed", not "Call ended"). */
  endedStatus?: string | null
  /** Room state lifted to the modal (close blocking, Intervene disabling). */
  onStatus?: (status: RoomStatus) => void
  /** Token fetch failed (e.g. 409 while another supervisor holds the mic); modal falls back to listen-only. */
  onJoinFailed?: (error: unknown) => void
}) {
  const microphone = mode !== "listen"
  const [join, setJoin] = useState<JoinTokenResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [reconnectNonce, setReconnectNonce] = useState(0)
  // Another window of ours claimed this identity and LiveKit evicted us. Latched, and it
  // gates `connect` — reconnecting would re-present the identity and evict THEM, who would
  // reconnect in turn: the seat ping-pongs and the roster flickers with it. Whichever layer
  // wants to retry (SDK leave-action, an effect re-run), this is where it stops.
  const [replaced, setReplaced] = useState(false)

  useEffect(() => {
    if (ended) return
    let cancelled = false
    getJoinToken(callId, mode)
      .then((res) => {
        if (!cancelled) setJoin(res)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e instanceof ApiError ? e.message : "Could not join the call.")
        onJoinFailed?.(e)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // onJoinFailed is intentionally not a dep: modals pass a fresh closure each render, which would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId, mode, ended, reconnectNonce])

  if (ended) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm">
        <PhoneOff className="size-4 text-red-500" />
        <span className="font-medium text-foreground">{terminalStatusMessage(endedStatus)}</span>
      </div>
    )
  }
  if (error) {
    return <div className="flex items-center justify-center p-6 text-sm text-destructive">{error}</div>
  }
  if (loading || !join) {
    return (
      <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Connecting…
      </div>
    )
  }
  // While intervening, a blocked mic or connect error would leave a dead panel with the
  // agent already silenced and the modal close-locked — fall back to listen-only instead.
  const handleRoomError = (e: unknown) => {
    if (microphone) onJoinFailed?.(e)
    else setError(e instanceof Error ? e.message : "Call connection failed.")
  }
  const reconnect = () => {
    setError(null)
    setJoin(null)
    setReplaced(false)
    setReconnectNonce((n) => n + 1)
  }
  return (
    <LiveKitRoom
      serverUrl={join.url}
      token={join.token}
      connect={!replaced}
      audio={microphone}
      video={false}
      onDisconnected={(reason) => {
        if (reason === DisconnectReason.DUPLICATE_IDENTITY) setReplaced(true)
      }}
      onMediaDeviceFailure={() => handleRoomError(new Error("microphone unavailable"))}
      onError={handleRoomError}
      // !h-auto: .lk-room-container's height:100% would collapse the transcript below.
      className="flex !h-auto flex-col"
    >
      <LiveActivityBeacon />
      <RoomView
        microphone={microphone}
        onStatus={onStatus}
        ended={ended}
        onReconnect={reconnect}
        replaced={replaced}
      />
    </LiveKitRoom>
  )
}
