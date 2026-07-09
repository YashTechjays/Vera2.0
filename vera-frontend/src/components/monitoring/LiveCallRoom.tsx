import { useEffect, useState } from "react"
import { Loader2, Radio } from "lucide-react"
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useParticipants,
} from "@livekit/components-react"
import { ConnectionState } from "livekit-client"

import { ApiError } from "@/lib/api/client"
import { getJoinToken, type JoinTokenResponse } from "@/lib/api/calls"

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  [ConnectionState.Disconnected]: "Disconnected",
  [ConnectionState.Connecting]: "Connecting…",
  [ConnectionState.Connected]: "Connected",
  [ConnectionState.Reconnecting]: "Reconnecting…",
  [ConnectionState.SignalReconnecting]: "Reconnecting…",
}

function RoomState() {
  const state = useConnectionState()
  const participants = useParticipants()
  const connected = state === ConnectionState.Connected
  return (
    <div className="flex flex-1 flex-col gap-3 p-4 text-sm">
      <div className="flex items-center gap-2">
        <Radio className={connected ? "size-4 text-emerald-600" : "size-4 text-muted-foreground"} />
        <span className="font-medium">{CONNECTION_LABEL[state]}</span>
      </div>
      <div>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Participants ({participants.length})
        </p>
        {participants.length === 0 ? (
          <p className="text-muted-foreground">Waiting for participants…</p>
        ) : (
          <ul className="space-y-1 text-muted-foreground">
            {participants.map((p) => (
              <li key={p.sid} className="font-mono text-xs">
                {p.identity}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

/**
 * Joins a call's LiveKit room via a server-minted token and shows the live
 * connection + participants. Drop-in for a modal's "live" panel; unmounting it
 * (closing the modal) disconnects the participant.
 */
export function LiveCallRoom({
  callId,
  microphone = false,
}: {
  callId: string
  /** Enable the local mic (intervene only). Watch views must leave this off —
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
  return (
    <LiveKitRoom
      serverUrl={join.url}
      token={join.token}
      connect
      audio={microphone && !micFailed}
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
      <RoomState />
      <RoomAudioRenderer />
    </LiveKitRoom>
  )
}
