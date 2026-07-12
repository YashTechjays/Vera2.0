import { useEffect, useState } from "react"
import { Loader2, Mic, MicOff, Radio, Volume2, VolumeX } from "lucide-react"
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useAudioPlayback,
  useConnectionState,
  useParticipantAttributes,
  useParticipants,
  useTrackToggle,
} from "@livekit/components-react"
import { ConnectionState, ParticipantKind, Track, type Participant } from "livekit-client"

import { Button } from "@/components/ui/button"
import { ApiError } from "@/lib/api/client"
import { getJoinToken, type JoinTokenResponse } from "@/lib/api/calls"
import {
  CONNECTION_PHASE_LABEL,
  PARTICIPANT_MODE_BADGE,
  connectionPhase,
  isWaitingForCall,
  otherIntervenerPresent,
  participantLabel,
  participantMode,
  speakerButtonState,
  type ConnectionPhase,
  type ParticipantLike,
  type ParticipantMode,
  type RoomStatus,
} from "@/lib/monitoring/liveCallView"

const PHASE_ICON_CLASS: Record<ConnectionPhase, string> = {
  connecting: "text-muted-foreground",
  live: "text-emerald-600",
  reconnecting: "text-amber-600",
  ended: "text-muted-foreground",
}

const MODE_BADGE_CLASS: Record<ParticipantMode, string> = {
  intervener: "text-amber-600",
  listener: "text-muted-foreground",
  agent: "text-emerald-600",
  callee: "text-muted-foreground",
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

/** Mic mute/unmute. Rendered in every mode; disabled while listen-only (the
 *  token cannot publish — the server, not this button, keeps listeners mute). */
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

function RoomView({
  microphone,
  onStatus,
}: {
  microphone: boolean
  onStatus?: (status: RoomStatus) => void
}) {
  const state = useConnectionState()
  const participants = useParticipants()
  const [outputMuted, setOutputMuted] = useState(false)
  // Latch (adjusted during render): once connected, a later disconnect means
  // the room is gone, not that the initial connect is still in flight.
  const [everConnected, setEverConnected] = useState(false)
  if (state === ConnectionState.Connected && !everConnected) setEverConnected(true)

  const likes = participants.map(toParticipantLike)
  const phase = connectionPhase(state, everConnected)
  const otherIntervener = otherIntervenerPresent(likes)

  useEffect(() => {
    onStatus?.({ phase, otherIntervener })
  }, [onStatus, phase, otherIntervener])

  return (
    <div className="flex flex-1 flex-col gap-3 p-4 text-sm">
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
      {isWaitingForCall(state, likes) && (
        <p className="text-muted-foreground">Waiting for the call…</p>
      )}
      {participants.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Participants ({participants.length})
          </p>
          <ul className="space-y-1">
            {participants.map((p) => (
              <ParticipantRow key={p.sid} participant={p} />
            ))}
          </ul>
        </div>
      )}
      <RoomAudioRenderer muted={outputMuted} />
    </div>
  )
}

/**
 * Joins a call's LiveKit room via a server-minted token and renders the live
 * panel: connection phase, all participants (email + join-mode badge), a
 * speaker toggle (autoplay unlock + output mute), and a mic toggle (active
 * while intervening). Unmounting (closing the modal) disconnects the
 * participant.
 *
 * Changing `microphone` needs a NEW token with different grants, and LiveKit
 * ignores a token swap while connected — the parent must remount this
 * component (key it on the mode) to switch.
 */
export function LiveCallRoom({
  callId,
  microphone = false,
  onStatus,
  onJoinFailed,
}: {
  callId: string
  /** Publish the local mic (intervene only). Watch views must leave this off —
   *  a viewer must never be audible in the room, and requesting mic access
   *  fails outright where getUserMedia is blocked (e.g. incognito). */
  microphone?: boolean
  /** Room state lifted to the modal (close blocking, Intervene disabling). */
  onStatus?: (status: RoomStatus) => void
  /** Token fetch failed — e.g. 409 while another supervisor holds the mic.
   *  The modal uses it to fall back to listen-only. */
  onJoinFailed?: (error: unknown) => void
}) {
  const [join, setJoin] = useState<JoinTokenResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [micFailed, setMicFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    getJoinToken(callId, microphone)
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
    // onJoinFailed is deliberately not a dependency: modals pass a fresh
    // closure each render and a re-fetch on that would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId, microphone])

  if (error) {
    return <div className="flex flex-1 items-center justify-center p-6 text-sm text-destructive">{error}</div>
  }
  if (loading || !join) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Connecting…
      </div>
    )
  }
  // Publish the mic only when intervening AND the device is available.
  const micActive = microphone && !micFailed
  return (
    <LiveKitRoom
      serverUrl={join.url}
      token={join.token}
      connect
      audio={micActive}
      video={false}
      // A blocked mic must not kill the panel — fall back to listen-only.
      onMediaDeviceFailure={() => setMicFailed(true)}
      onError={(e) => setError(e.message)}
      className="flex flex-1 flex-col"
    >
      {micFailed && (
        <p className="px-4 pt-3 text-xs text-amber-600">
          Microphone unavailable — listening only.
        </p>
      )}
      <RoomView microphone={micActive} onStatus={onStatus} />
    </LiveKitRoom>
  )
}
