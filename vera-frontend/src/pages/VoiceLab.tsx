import { useState } from "react"
import { Mic, PhoneOutgoing, Radio } from "lucide-react"
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useParticipants,
} from "@livekit/components-react"
import { ConnectionState } from "livekit-client"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ApiError } from "@/lib/api/client"
import {
  endVoiceSession,
  startVoiceSession,
  type VoiceSessionMode,
  type VoiceSessionResponse,
} from "@/lib/api/voiceLab"

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  [ConnectionState.Disconnected]: "Disconnected",
  [ConnectionState.Connecting]: "Connecting…",
  [ConnectionState.Connected]: "Connected",
  [ConnectionState.Reconnecting]: "Reconnecting…",
  [ConnectionState.SignalReconnecting]: "Reconnecting…",
}

/** Renders live connection + participant state. Must live inside <LiveKitRoom>
 *  so the LiveKit room context is available to its hooks. */
function SessionPanel({ mode, onEnd }: { mode: VoiceSessionMode; onEnd: () => void }) {
  const state = useConnectionState()
  const participants = useParticipants()

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Radio className="size-4 text-emerald-600" />
          Live session
          <span className="text-sm font-normal text-muted-foreground">
            ({mode === "browser" ? "in-browser — mic on" : "outbound — listen-only"})
          </span>
        </CardTitle>
        <Button variant="destructive" onClick={onEnd}>
          End session
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="text-sm">
          Connection: <span className="font-medium">{CONNECTION_LABEL[state]}</span>
        </div>
        <div>
          <p className="mb-1 text-sm font-medium">Participants</p>
          {participants.length === 0 ? (
            <p className="text-sm text-muted-foreground">Waiting for participants…</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {participants.map((p) => (
                <li key={p.sid} className="text-muted-foreground">
                  {p.identity}
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export function VoiceLab() {
  const [phone, setPhone] = useState("")
  const [session, setSession] = useState<VoiceSessionResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<VoiceSessionMode | null>(null)

  async function start(mode: VoiceSessionMode) {
    setError(null)
    setPending(mode)
    try {
      const result = await startVoiceSession(
        mode === "outbound" ? { mode, phone_number: phone.trim() } : { mode },
      )
      setSession(result)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the session.")
    } finally {
      setPending(null)
    }
  }

  async function endSession() {
    const roomName = session?.room_name
    // Drop the browser out of the room immediately, then tell the backend to delete
    // the room so the agent worker and any outbound SIP call are torn down too —
    // leaving the browser alone would leave both running.
    setSession(null)
    setError(null)
    if (roomName) {
      try {
        await endVoiceSession(roomName)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not end the session cleanly.")
      }
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Voice Lab</h1>
        <p className="text-sm text-muted-foreground">
          Start a voice session with the Vera agent and listen to it live — talk in the
          browser, or dial out to a phone number over SIP.
        </p>
      </div>

      {session ? (
        <LiveKitRoom
          serverUrl={session.url}
          token={session.token}
          connect
          audio={session.mode === "browser"}
          video={false}
          onError={(e) => setError(e.message)}
        >
          <SessionPanel mode={session.mode} onEnd={endSession} />
          <RoomAudioRenderer />
        </LiveKitRoom>
      ) : (
        <Card>
          <CardContent className="space-y-5 py-6">
            <div className="space-y-2">
              <Label htmlFor="phone">Phone number (E.164, outbound only)</Label>
              <Input
                id="phone"
                type="tel"
                placeholder="+15551234567"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                className="max-w-xs"
              />
            </div>

            <div className="flex flex-wrap gap-3">
              <Button onClick={() => start("browser")} disabled={pending !== null}>
                <Mic /> Start in-browser session
              </Button>
              <Button
                variant="outline"
                onClick={() => start("outbound")}
                disabled={pending !== null || phone.trim() === ""}
              >
                <PhoneOutgoing /> Start outbound call
              </Button>
            </div>

            {error && <p className="text-sm text-destructive">{error}</p>}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
