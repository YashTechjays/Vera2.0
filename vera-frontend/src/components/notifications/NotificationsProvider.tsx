import { useEffect, type ReactNode } from "react"
import { Toaster, toast } from "sonner"

import { asInterventionNeeded, streamNotifications } from "@/lib/api/notifications"

/** Window event fired on every received notification — pages that show live
 *  call state (Live Monitoring) listen and refetch immediately instead of
 *  waiting for the next poll. */
export const NOTIFICATION_EVENT = "vera:notification"

const FLAG_LABELS: Record<string, string> = {
  supervisor_requested: "Supervisor requested",
  repeated_questions: "Repeated questions",
  hallucination: "Possible hallucination",
  conversation_loop: "Conversation loop",
  long_silence: "Long silence",
  off_script: "Off script",
  low_confidence: "Low confidence",
  other: "Needs attention",
}

/**
 * Login-session realtime notifications: opens ONE SSE for the whole session
 * (mounted around the authenticated shell) and toasts intervention alerts.
 * The toast carries category + score only — never patient details or the LLM
 * reason (PHI hygiene: minimum necessary on a surface that outlives the page).
 * A 4xx (expired session / revoked permission) stops the stream quietly;
 * RequireAuth handles the redirect on the next API call.
 */
export function NotificationsProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    const controller = new AbortController()
    streamNotifications({
      signal: controller.signal,
      onNotification: (n) => {
        window.dispatchEvent(new CustomEvent(NOTIFICATION_EVENT))
        const alert = asInterventionNeeded(n)
        if (alert) {
          toast.warning("Call needs intervention", {
            description: `${FLAG_LABELS[alert.flag] ?? alert.flag} — health ${alert.score}%`,
          })
        }
      },
    }).catch(() => {
      // Non-retryable (4xx). Silent: the session flow owns re-auth UX.
    })
    return () => controller.abort()
  }, [])

  return (
    <>
      {children}
      <Toaster position="top-right" richColors closeButton />
    </>
  )
}
