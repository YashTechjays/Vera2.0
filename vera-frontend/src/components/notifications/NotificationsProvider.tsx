import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { useNavigate } from "react-router-dom"
import { Toaster, toast } from "sonner"

import { asInterventionNeeded, streamNotifications } from "@/lib/api/notifications"
import {
  FLAG_LABELS,
  addItem,
  compareEntryIds,
  latestEntryId,
  loadReadCursor,
  saveReadCursor,
  shortCallRef,
  unreadCount,
  type NotificationItem,
  type OpenCallNavState,
} from "@/lib/notifications/store"
import { NotificationsContext } from "./context"

/** Window event fired on every received notification — pages that show live
 *  call state (Live Monitoring) listen and refetch immediately instead of
 *  waiting for the next poll. */
export const NOTIFICATION_EVENT = "vera:notification"

/**
 * Login-session realtime notifications: opens ONE SSE for the whole session
 * (mounted around the authenticated shell), toasts intervention alerts, and
 * feeds the bell inbox. The toast/badge carry category + score only — never
 * patient details or the LLM reason (PHI hygiene: minimum necessary on a
 * surface that outlives the page).
 *
 * Replay-idempotence: the server replays a short window on every (re)connect
 * so nothing is missed during a reload gap. Overlap is made harmless twice
 * over — an in-memory seen-set drops same-mount duplicates, and the PERSISTED
 * read cursor (sessionStorage, opaque stream ids only) keeps already-read
 * alerts from re-toasting or re-counting as unread across reloads.
 *
 * A 4xx (expired session / revoked permission) stops the stream quietly;
 * RequireAuth handles the redirect on the next API call.
 */
export function NotificationsProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<NotificationItem[]>([])
  const [cursor, setCursor] = useState<string | null>(() => loadReadCursor())
  const cursorAtMount = useRef(cursor) // the stream callback compares replays against this
  const seenIds = useRef<Set<string>>(new Set())
  const navigate = useNavigate()

  useEffect(() => {
    const controller = new AbortController()
    streamNotifications({
      signal: controller.signal,
      onNotification: (n) => {
        if (n.id) {
          if (seenIds.current.has(n.id)) return
          if (seenIds.current.size >= 500) seenIds.current.clear() // bound memory
          seenIds.current.add(n.id)
        }
        window.dispatchEvent(new CustomEvent(NOTIFICATION_EVENT))
        const alert = asInterventionNeeded(n)
        if (!alert) return
        setItems((prev) =>
          addItem(prev, {
            id: n.id,
            callId: alert.callId,
            flag: alert.flag,
            score: alert.score,
            ts: n.ts,
          }),
        )
        // A replayed, already-read alert refills the inbox history but must not
        // toast again (reload inside the replay window).
        const alreadyRead =
          n.id !== "" &&
          cursorAtMount.current !== null &&
          compareEntryIds(n.id, cursorAtMount.current) <= 0
        if (!alreadyRead) {
          // shortCallRef (a non-PHI fragment of the call's opaque id) leads the
          // title — with several concurrent alerts, that's the one glanceable
          // detail that tells them apart; the message shouldn't bury it at the end.
          toast.warning(`${shortCallRef(alert.callId)} Call needs intervention`, {
            description: `${FLAG_LABELS[alert.flag] ?? alert.flag} — health ${alert.score}%`,
            action: {
              label: "View",
              onClick: () => {
                const state: OpenCallNavState = { openCallId: alert.callId }
                void navigate("/", { state })
              },
            },
          })
        }
      },
    }).catch(() => {
      // Non-retryable (4xx). Silent: the session flow owns re-auth UX.
    })
    return () => controller.abort()
    // `navigate` (react-router's useNavigate()) is a stable reference across
    // renders, so listing it here satisfies exhaustive-deps without causing
    // this effect to re-run/reopen the SSE connection on every render.
  }, [navigate])

  const markAllRead = useCallback(() => {
    const latest = latestEntryId(items)
    if (latest === null || (cursor !== null && compareEntryIds(latest, cursor) <= 0)) return
    saveReadCursor(latest)
    setCursor(latest)
  }, [items, cursor])

  const clearAll = useCallback(() => {
    markAllRead()
    setItems([])
  }, [markAllRead])

  return (
    <NotificationsContext.Provider
      value={{ items, cursor, unread: unreadCount(items, cursor), markAllRead, clearAll }}
    >
      {children}
      <Toaster position="top-right" richColors closeButton />
    </NotificationsContext.Provider>
  )
}
