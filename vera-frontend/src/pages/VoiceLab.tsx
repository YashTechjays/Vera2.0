import { useCallback, useEffect, useRef, useState } from "react"
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
import { streamTranscription, type TranscriptEvent } from "@/lib/api/transcription"

/** Visibility of the "Start in-browser session" button. Hidden by default.
 *  Two ways to bring it back:
 *   - Permanently, in source: set DEFAULT to `true` and rebuild.
 *   - Ad hoc in a deployed browser, no redeploy: run
 *       localStorage.setItem("vera.showBrowserSession", "1")
 *     in the devtools console and reload (removeItem to hide it again).
 *  A build-time const alone can't do the second — minification inlines `false`
 *  and drops the dead branch from the bundle — so the runtime localStorage check
 *  is what keeps the in-browser unhide possible. */
const SHOW_IN_BROWSER_SESSION_DEFAULT: boolean = false
const SHOW_IN_BROWSER_SESSION =
  SHOW_IN_BROWSER_SESSION_DEFAULT || localStorage.getItem("vera.showBrowserSession") === "1"

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
  const wasConnected = useRef(false)

  useEffect(() => {
    if (state === ConnectionState.Connected) {
      wasConnected.current = true
    }
    // Auto-cleanup: if we were connected and the room disconnected (agent deleted it,
    // network drop, etc.), clear the session so the UI resets to the start form.
    if (wasConnected.current && state === ConnectionState.Disconnected) {
      onEnd()
    }
  }, [state, onEnd])

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

function TranscriptPanel({ roomName }: { roomName: string }) {
  const [turns, setTurns] = useState<TranscriptEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    streamTranscription(roomName, {
      signal: controller.signal,
      onEvent: (e) => setTurns((prev) => [...prev, e]),
    }).catch((err) => {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : "Transcript stream failed.")
      }
    })
    return () => controller.abort()
  }, [roomName])

  return (
    <Card>
      <CardHeader>
        <CardTitle>Live transcript</CardTitle>
      </CardHeader>
      <CardContent className="max-h-80 space-y-2 overflow-y-auto text-sm">
        {turns.length === 0 && !error && (
          <p className="text-muted-foreground">Waiting for the conversation…</p>
        )}
        {turns.map((t, i) => (
          <div key={i}>
            <span
              className={
                t.role === "agent"
                  ? "font-medium text-emerald-700"
                  : "font-medium text-foreground"
              }
            >
              {t.role === "agent" ? "Agent" : "Caller"}:
            </span>{" "}
            <span className="text-muted-foreground">{t.text}</span>
          </div>
        ))}
        {error && <p className="text-destructive">{error}</p>}
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

  const endSession = useCallback(async () => {
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
  }, [session?.room_name])

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
          <TranscriptPanel key={session.room_name} roomName={session.room_name} />
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
              {SHOW_IN_BROWSER_SESSION && (
                <Button onClick={() => start("browser")} disabled={pending !== null}>
                  <Mic /> Start in-browser session
                </Button>
              )}
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
