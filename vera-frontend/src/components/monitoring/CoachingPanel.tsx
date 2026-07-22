import { useEffect, useRef, useState } from "react"
import { Mic, Send, Square } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ApiError } from "@/lib/api/client"
import { sendCoachMessage, transcribeWhisper, type CoachOrigin } from "@/lib/api/coaching"

// backend allows up to 2000; this is a tighter UI guardrail
const MAX_MESSAGE_LENGTH = 200

/**
 * Coaching input: type a note, or click the mic to whisper it (click again to
 * stop and transcribe). Both paths land on the same box — a completed whisper
 * transcription just fills it in for review/edit before the supervisor hits
 * Send. Independent of Intervene: this renders whenever the caller may coach,
 * in both listen and intervene mode.
 */
export function CoachingPanel({
  callId,
  disabled = false,
}: {
  callId: string
  disabled?: boolean
}) {
  const [message, setMessage] = useState("")
  const [origin, setOrigin] = useState<CoachOrigin>("typed")
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [truncated, setTruncated] = useState(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  // Guards the getUserMedia race: a tap short enough that the mic isn't ready
  // until after release would otherwise leave a stream open with no way to
  // stop it. mountedRef covers the same race on unmount.
  const startingRef = useRef(false)
  const stopRequestedRef = useRef(false)
  const mountedRef = useRef(true)
  const uploadAbortRef = useRef<AbortController | null>(null)

  const busy = recording || transcribing || sending

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      recorderRef.current?.stop() // releases the mic if a recording is still open
      uploadAbortRef.current?.abort() // don't let an in-flight whisper upload outlive the panel
    }
  }, [])

  function handleMessageChange(next: string) {
    setMessage(next)
    setTruncated(false)
    // Clearing the box is treated as starting a fresh note — otherwise a
    // whisper-origin tag would stick around after the supervisor deletes the
    // transcription and types something unrelated.
    if (next === "") setOrigin("typed")
  }

  async function startRecording() {
    if (startingRef.current || recorderRef.current) return // already starting/recording
    startingRef.current = true
    stopRequestedRef.current = false
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const recorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" })
      chunksRef.current = []
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data)
      }
      // Release the mic on whichever event ends the recording — a mid-record
      // failure (device unplugged, stream goes bad) fires "error" instead of
      // "stop", and the stream must not leak on that path either.
      const releaseStream = () => {
        for (const track of stream.getTracks()) track.stop()
      }
      recorder.addEventListener("stop", releaseStream, { once: true })
      recorder.addEventListener("error", releaseStream, { once: true })
      recorder.start()
      recorderRef.current = recorder
      // The user already released (or the component unmounted) while the mic
      // was still starting up - stop it right away instead of leaving it open.
      if (stopRequestedRef.current || !mountedRef.current) {
        recorder.stop()
        recorderRef.current = null
        if (mountedRef.current) setRecording(false)
      } else {
        setRecording(true)
      }
    } catch {
      if (mountedRef.current) setError("Microphone access denied.")
    } finally {
      startingRef.current = false
    }
  }

  async function stopRecordingAndTranscribe() {
    stopRequestedRef.current = true
    const recorder = recorderRef.current
    if (!recorder) return // still starting up - startRecording will stop itself
    recorderRef.current = null
    setRecording(false)
    setTranscribing(true)
    try {
      const blob = await new Promise<Blob>((resolve, reject) => {
        recorder.addEventListener(
          "stop",
          () => resolve(new Blob(chunksRef.current, { type: "audio/webm" })),
          { once: true },
        )
        recorder.addEventListener(
          "error",
          () => reject(new Error("Recording failed.")),
          { once: true },
        )
        recorder.stop()
      })
      const controller = new AbortController()
      uploadAbortRef.current = controller
      const { text } = await transcribeWhisper(callId, blob, controller.signal)
      if (!mountedRef.current) return
      setTruncated(text.length > MAX_MESSAGE_LENGTH)
      setMessage(text.slice(0, MAX_MESSAGE_LENGTH))
      setOrigin("whisper")
    } catch (e) {
      if (mountedRef.current) {
        setError(e instanceof ApiError ? e.message : "Could not transcribe.")
      }
    } finally {
      uploadAbortRef.current = null
      if (mountedRef.current) setTranscribing(false)
    }
  }

  function handleMicClick() {
    // startingRef covers the gap between clicking start and `recording`
    // actually flipping true (getUserMedia hasn't resolved yet) - without it,
    // a quick second click here would be misread as "start" again.
    if (recording || startingRef.current) {
      void stopRecordingAndTranscribe()
    } else {
      void startRecording()
    }
  }

  function statusHint(): string {
    if (error !== null) return error
    if (transcribing) return "Transcribing…"
    if (recording) return "Recording — click to stop and transcribe"
    if (truncated) return `Cut to ${MAX_MESSAGE_LENGTH} characters — review before sending.`
    if (origin === "whisper" && message) return "Review the transcription, then send."
    return ""
  }

  async function handleSend() {
    const trimmed = message.trim()
    if (!trimmed) return
    setSending(true)
    setError(null)
    try {
      await sendCoachMessage(callId, trimmed, origin)
      setMessage("")
      setOrigin("typed")
      setTruncated(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not send coaching note.")
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="flex flex-col gap-1 border-t border-border bg-white px-3 py-2">
      <div className="flex items-center gap-2">
        <Input
          value={message}
          onChange={(e) => handleMessageChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault()
              void handleSend()
            }
          }}
          placeholder="Coach Vera — she'll weave this into her next reply…"
          maxLength={MAX_MESSAGE_LENGTH}
          disabled={disabled || busy}
        />
        <Button
          type="button"
          size="icon"
          variant={recording ? "destructive" : "outline"}
          disabled={disabled || transcribing || sending}
          title={recording ? "Click to stop and transcribe" : "Click to whisper"}
          onClick={handleMicClick}
        >
          {recording ? <Square className="size-3.5" /> : <Mic className="size-4" />}
        </Button>
        <Button
          type="button"
          size="icon"
          disabled={disabled || busy || !message.trim()}
          title="Send coaching note"
          onClick={() => void handleSend()}
        >
          <Send className="size-4" />
        </Button>
      </div>
      <div className="flex h-4 items-center justify-between text-xs">
        <span className={error ? "text-destructive" : "text-muted-foreground"}>{statusHint()}</span>
        {message && (
          <span className="text-muted-foreground">
            {message.length}/{MAX_MESSAGE_LENGTH}
          </span>
        )}
      </div>
    </div>
  )
}
