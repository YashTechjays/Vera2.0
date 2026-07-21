import { createContext, useContext } from "react"

import type { NotificationItem } from "@/lib/notifications/store"

export type NotificationsContextValue = {
  /** Newest-first bell inbox (session-scoped memory — never persisted). */
  items: NotificationItem[]
  /** Read cursor: items at or below it are read. */
  cursor: string | null
  unread: number
  /** Advance the cursor past everything currently in the inbox. */
  markAllRead: () => void
  /** Empty the inbox (also marks everything read). */
  clearAll: () => void
}

export const NotificationsContext = createContext<NotificationsContextValue | null>(null)

export function useNotifications(): NotificationsContextValue {
  const value = useContext(NotificationsContext)
  if (value === null) throw new Error("useNotifications requires NotificationsProvider")
  return value
}
