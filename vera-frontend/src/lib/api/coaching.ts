// Coaching mode: a supervisor sends Vera a short instruction (typed, or spoken via
// hold-to-whisper) that she folds into her next reply — the customer never hears or
// sees it. Same permission surface as Intervene; both endpoints share one 15/min
// rate-limit budget per call (server-enforced — see the /coach 429 response).

import { apiRequest } from "@/lib/api/client"

export type CoachOrigin = "typed" | "whisper"

/** POST /calls/{id}/coach — folds `message` into Vera's next reply. `origin`
 *  is internal bookkeeping only (typed vs. transcribed-then-reviewed whisper);
 *  the UI renders both the same way. */
export function sendCoachMessage(
  callId: string,
  message: string,
  origin: CoachOrigin = "typed",
): Promise<null> {
  return apiRequest<null>(`/calls/${encodeURIComponent(callId)}/coach`, {
    method: "POST",
    body: { message, origin },
  })
}

export type WhisperTranscribeResponse = {
  text: string
}

/** POST /calls/{id}/on-demand-transcribe — transcribes a hold-to-whisper
 *  recording; the caller reviews/edits the returned text and sends it via
 *  `sendCoachMessage(callId, text, "whisper")` themselves. Does not itself
 *  record anything — only an actual send does. */
export function transcribeWhisper(
  callId: string,
  audio: Blob,
  signal?: AbortSignal,
): Promise<WhisperTranscribeResponse> {
  const form = new FormData()
  form.append("audio", audio, "whisper.webm")
  return apiRequest<WhisperTranscribeResponse>(
    `/calls/${encodeURIComponent(callId)}/on-demand-transcribe`,
    { method: "POST", body: form, signal },
  )
}
