import { useEffect, useState } from "react"
import { Loader2, Mic, MicOff, Radio, Volume2, VolumeX } from "lucide-react"
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useAudioPlayback,
  useConnectionState,
  useParticipantPermissions,
  useRemoteParticipants,
  useTrackToggle,
} from "@livekit/components-react"
import { ConnectionState, Track, type RemoteParticipant } from "livekit-client"

import { Button } from "@/components/ui/button"
import { ApiError } from "@/lib/api/client"
import { getJoinToken, type JoinTokenResponse } from "@/lib/api/calls"
import {
  CONNECTION_PHASE_LABEL,
  connectionPhase,
  isWaitingForCall,
  participantModeLabel,
  speakerButtonState,
  type ConnectionPhase,
} from "@/lib/monitoring/liveCallView"

const PHASE_ICON_CLASS: Record<ConnectionPhase, string> = {
  connecting: "text-muted-foreground",
  live: "text-emerald-600",
  reconnecting: "text-amber-600",
  ended: "text-muted-foreground",
}

function ParticipantRow({ participant }: { participant: RemoteParticipant }) {
  const permissions = useParticipantPermissions({ participant })
  return (
    <li className="flex items-center justify-between gap-2">
      <span className="font-mono text-xs">{participant.identity}</span>
      <span className="text-xs text-muted-foreground">
        {participantModeLabel(permissions?.canPublish)}
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

function MicToggle() {
  const { enabled, pending, toggle } = useTrackToggle({ source: Track.Source.Microphone })
  const title = enabled ? "Mute microphone" : "Unmute microphone"
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      title={title}
      aria-label={title}
      disabled={pending}
      onClick={() => void toggle()}
    >
      {enabled ? <Mic className="size-4" /> : <MicOff className="size-4" />}
    </Button>
  )
}

function RoomView({ microphone }: { microphone: boolean }) {
  const state = useConnectionState()
  const remotes = useRemoteParticipants()
  const [outputMuted, setOutputMuted] = useState(false)
  // Latch (adjusted during render): once connected, a later disconnect means
  // the room is gone, not that the initial connect is still in flight.
  const [everConnected, setEverConnected] = useState(false)
  if (state === ConnectionState.Connected && !everConnected) setEverConnected(true)

  const phase = connectionPhase(state, everConnected)
  return (
    <div className="flex flex-1 flex-col gap-3 p-4 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Radio className={`size-4 ${PHASE_ICON_CLASS[phase]}`} />
          <span className="font-medium">{CONNECTION_PHASE_LABEL[phase]}</span>
        </div>
        <div className="flex items-center gap-1">
          <SpeakerToggle outputMuted={outputMuted} onOutputMutedChange={setOutputMuted} />
          {microphone && <MicToggle />}
        </div>
      </div>
      {isWaitingForCall(state, remotes.length) ? (
        <p className="text-muted-foreground">Waiting for the call…</p>
      ) : (
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Participants ({remotes.length})
          </p>
          <ul className="space-y-1">
            {remotes.map((p) => (
              <ParticipantRow key={p.sid} participant={p} />
            ))}
          </ul>
        </div>
      )}
      <RoomAudioRenderer volume={outputMuted ? 0 : 1} />
    </div>
  )
}

/**
 * Joins a call's LiveKit room via a server-minted token and renders the live
 * panel: connection phase, remote participants with their speak/listen mode,
 * a speaker toggle (autoplay unlock + output mute), and — when intervening —
 * a mic toggle. Unmounting (closing the modal) disconnects the participant.
 *
 * Changing `microphone` needs a NEW token with different grants, and LiveKit
 * ignores a token swap while connected — the parent must remount this
 * component (key it on the mode) to switch.
 */
export function LiveCallRoom({
  callId,
  microphone = false,
}: {
  callId: string
  /** Publish the local mic (intervene only). Watch views must leave this off —
   *  a viewer must never be audible in the room, and requesting mic access
   *  fails outright where getUserMedia is blocked (e.g. incognito). */
  microphone?: boolean
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
        if (!cancelled) setError(e instanceof ApiError ? e.message : "Could not join the call.")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
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
      <RoomView microphone={micActive} />
    </LiveKitRoom>
  )
}
