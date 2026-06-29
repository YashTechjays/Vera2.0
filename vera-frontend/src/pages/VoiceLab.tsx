import { useEffect, useState } from "react"
import { AlertTriangle, Mic, PhoneOutgoing, Radio } from "lucide-react"
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useParticipants,
} from "@livekit/components-react"
import { ConnectionState } from "livekit-client"
// `/max` metadata so isValidPhoneNumber does real per-country validation (length +
// national pattern), not just E.164 shape. The component yields an E.164 string,
// handling leading-zero / trunk-prefix stripping the old hand-rolled helper got wrong.
import PhoneInput, { isValidPhoneNumber } from "react-phone-number-input/max"
import "react-phone-number-input/style.css"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
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

/** Inline destructive banner for a request/session error. Renders nothing when
 *  there's no error, so call sites stay a one-liner. */
function ErrorAlert({ error }: { error: string | null }) {
  if (!error) return null
  return (
    <Alert variant="destructive">
      <AlertTriangle />
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  )
}

export function VoiceLab() {
  // The PhoneInput yields an E.164 string (e.g. "+15551234567") or undefined.
  const [phone, setPhone] = useState<string | undefined>(undefined)
  // Only flag the number field once the operator has interacted with it, so an
  // untouched empty form doesn't show a red error.
  const [touched, setTouched] = useState(false)
  const [session, setSession] = useState<VoiceSessionResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<VoiceSessionMode | null>(null)

  // Real per-country validation client-side; the backend E.164 regex remains the
  // source-of-truth gate, this just fails fast so an invalid number never round-trips.
  const phoneValid = !!phone && isValidPhoneNumber(phone)
  const showPhoneError = touched && !phoneValid

  async function start(mode: VoiceSessionMode) {
    setError(null)
    setPending(mode)
    try {
      const result = await startVoiceSession(
        mode === "outbound" ? { mode, phone_number: phone! } : { mode },
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

      {session && <ErrorAlert error={error} />}

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
              <Label htmlFor="phone">Phone number (outbound only)</Label>
              <PhoneInput
                id="phone"
                international
                defaultCountry="US"
                placeholder="Enter phone number"
                value={phone}
                onChange={setPhone}
                onBlur={() => setTouched(true)}
                aria-invalid={showPhoneError}
                className="max-w-md"
              />
              {showPhoneError ? (
                <p className="text-sm text-destructive">
                  Enter a valid phone number for the selected country.
                </p>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Pick the country, then enter the local number — we'll dial{" "}
                  <span className="font-medium">{phone || "…"}</span>.
                </p>
              )}
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
                disabled={pending !== null || !phoneValid}
              >
                <PhoneOutgoing /> Start outbound call
              </Button>
            </div>

            <ErrorAlert error={error} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
