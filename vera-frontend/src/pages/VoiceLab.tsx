import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { AlertTriangle, ListTree, Loader2, Mic, PhoneOutgoing, Radio } from "lucide-react"
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
import { Switch } from "@/components/ui/switch"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import {
  endVoiceSession,
  startVoiceSession,
  type VoiceSessionMode,
  type VoiceSessionResponse,
} from "@/lib/api/voiceLab"
import { streamTranscription, type TranscriptEvent } from "@/lib/api/transcription"
import { VoiceLabDialpad } from "@/components/voice-lab/VoiceLabDialpad"

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
function SessionPanel({
  mode,
  onEnd,
  actions,
}: {
  mode: VoiceSessionMode
  onEnd: () => void
  actions?: ReactNode
}) {
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
      <CardHeader className="flex flex-row items-center justify-between gap-3 space-y-0">
        <CardTitle className="flex items-center gap-2">
          <Radio className="size-4 text-emerald-600" />
          Live session
          <span className="text-sm font-normal text-muted-foreground">
            ({mode === "browser" ? "in-browser — mic on" : "outbound — listen-only"})
          </span>
        </CardTitle>
        <div className="flex items-center gap-2">
          {actions}
          <Button variant="destructive" size="sm" onClick={onEnd}>
            End session
          </Button>
        </div>
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
  const [ivrNavigation, setIvrNavigation] = useState(false)
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
      const result = await startVoiceSession({
        mode,
        ivr_navigation: ivrNavigation,
        ...(mode === "outbound" ? { phone_number: phone! } : {}),
      })
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
          <div className="space-y-6">
            <SessionPanel
              mode={session.mode}
              onEnd={endSession}
              actions={
                session.mode === "outbound" ? <VoiceLabDialpad onError={setError} /> : undefined
              }
            />
            <TranscriptPanel key={session.room_name} roomName={session.room_name} />
          </div>
          <RoomAudioRenderer />
        </LiveKitRoom>
      ) : (
        <Card>
          <CardContent className="max-w-lg space-y-5 py-6">
            <div className="flex items-start gap-3">
              <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted">
                <PhoneOutgoing className="size-4 text-muted-foreground" />
              </div>
              <div>
                <h2 className="text-sm font-medium leading-none">Outbound call</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Dial a phone number and listen to the Vera agent live over SIP.
                </p>
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="phone">Phone number</Label>
              {/* react-phone-number-input renders its own DOM (.PhoneInput /
                  .PhoneInputCountry / .PhoneInputInput), so we reach into those
                  internals with Tailwind arbitrary variants to make the widget
                  read as a single shadcn-style field: one bordered box, a flag
                  segment with a divider, a borderless number input, and a shared
                  focus-within ring. Colors use the design tokens so it tracks
                  light/dark automatically. */}
              <div
                className={cn(
                  // the field box
                  "[&_.PhoneInput]:flex [&_.PhoneInput]:h-9 [&_.PhoneInput]:items-stretch [&_.PhoneInput]:overflow-hidden",
                  "[&_.PhoneInput]:rounded-md [&_.PhoneInput]:border [&_.PhoneInput]:border-input [&_.PhoneInput]:bg-background",
                  "[&_.PhoneInput]:shadow-xs [&_.PhoneInput]:transition-[color,box-shadow]",
                  // shared focus ring (focus lands on the inner input)
                  "[&_.PhoneInput:focus-within]:border-ring [&_.PhoneInput:focus-within]:ring-[3px] [&_.PhoneInput:focus-within]:ring-ring/50",
                  // country segment: flag + dial code + caret, with a divider
                  "[&_.PhoneInputCountry]:m-0 [&_.PhoneInputCountry]:flex [&_.PhoneInputCountry]:items-center [&_.PhoneInputCountry]:gap-1.5",
                  "[&_.PhoneInputCountry]:border-r [&_.PhoneInputCountry]:border-input [&_.PhoneInputCountry]:px-2.5",
                  "[&_.PhoneInputCountrySelectArrow]:text-muted-foreground [&_.PhoneInputCountrySelectArrow]:opacity-80",
                  // borderless national-number input
                  "[&_.PhoneInputInput]:h-full [&_.PhoneInputInput]:min-w-0 [&_.PhoneInputInput]:flex-1 [&_.PhoneInputInput]:border-0",
                  "[&_.PhoneInputInput]:bg-transparent [&_.PhoneInputInput]:px-2.5 [&_.PhoneInputInput]:text-sm [&_.PhoneInputInput]:text-foreground [&_.PhoneInputInput]:outline-none",
                  "[&_.PhoneInputInput::placeholder]:text-muted-foreground",
                  // invalid + disabled read straight off the inner input's own
                  // aria-invalid / disabled via :has() — no duplicated wrapper state.
                  "[&_.PhoneInput:has(input[aria-invalid=true])]:border-destructive",
                  "[&_.PhoneInput:has(input[aria-invalid=true]):focus-within]:ring-destructive/20",
                  "[&_.PhoneInput:has(input:disabled)]:pointer-events-none [&_.PhoneInput:has(input:disabled)]:opacity-50",
                )}
              >
                <PhoneInput
                  id="phone"
                  international
                  defaultCountry="US"
                  placeholder="Enter phone number"
                  value={phone}
                  onChange={setPhone}
                  onBlur={() => setTouched(true)}
                  aria-invalid={showPhoneError}
                  aria-describedby="phone-hint"
                  disabled={pending !== null}
                />
              </div>
              {showPhoneError ? (
                <p id="phone-hint" role="alert" className="text-sm text-destructive">
                  Enter a valid phone number for the selected country.
                </p>
              ) : (
                <p id="phone-hint" className="text-sm text-muted-foreground">
                  Pick the country, then enter the local number — we'll dial{" "}
                  <span className="font-medium text-foreground">{phone || "…"}</span>.
                </p>
              )}
            </div>

            <div
              className={cn(
                "flex items-center justify-between gap-4 rounded-lg border p-3 transition-colors",
                ivrNavigation && "border-primary/40 bg-primary/5",
              )}
            >
              <div className="flex items-start gap-3">
                <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted">
                  <ListTree className="size-4 text-muted-foreground" />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="ivr-navigation" className="leading-none">
                    IVR navigation
                  </Label>
                  <p className="text-sm text-muted-foreground">
                    Let the agent navigate the payer's phone menu automatically before reaching
                    a rep.
                  </p>
                </div>
              </div>
              <Switch
                id="ivr-navigation"
                checked={ivrNavigation}
                onCheckedChange={setIvrNavigation}
              />
            </div>

            <div className="flex flex-wrap gap-3">
              <Button
                onClick={() => start("outbound")}
                disabled={pending !== null || !phoneValid}
              >
                {pending === "outbound" ? (
                  <>
                    <Loader2 className="animate-spin" /> Starting call…
                  </>
                ) : (
                  <>
                    <PhoneOutgoing /> Start outbound call
                  </>
                )}
              </Button>
              {SHOW_IN_BROWSER_SESSION && (
                <Button
                  variant="outline"
                  onClick={() => start("browser")}
                  disabled={pending !== null}
                >
                  {pending === "browser" ? (
                    <>
                      <Loader2 className="animate-spin" /> Starting…
                    </>
                  ) : (
                    <>
                      <Mic /> Start in-browser session
                    </>
                  )}
                </Button>
              )}
            </div>

            <ErrorAlert error={error} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
