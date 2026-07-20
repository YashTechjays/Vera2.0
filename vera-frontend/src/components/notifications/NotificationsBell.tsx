import { useEffect, useState } from "react"
import { Bell, BellOff } from "lucide-react"
import { useNavigate } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import {
  FLAG_LABELS,
  isUnread,
  shortCallRef,
  timeAgo,
  type NotificationItem,
  type OpenCallNavState,
} from "@/lib/notifications/store"
import { useNotifications } from "./context"

/**
 * Topbar bell: unread badge + a popover inbox of intervention alerts.
 * Closing the panel marks everything read (the badge clears); "Clear" empties
 * the inbox. Clicking an alert navigates to Live Monitoring and opens THAT
 * specific call's modal — critical with several concurrent flagged calls,
 * where "somewhere in the Critical tab" isn't good enough.
 */
export function NotificationsBell() {
  const { items, cursor, unread, markAllRead, clearAll } = useNotifications()
  const [open, setOpen] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const navigate = useNavigate()

  // Tick the relative timestamps while the panel is open.
  useEffect(() => {
    if (!open) return
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [open])

  function onOpenChange(next: boolean) {
    if (next) setNow(Date.now())
    if (!next) markAllRead() // panel closed = alerts seen
    setOpen(next)
  }

  function openCall(item: NotificationItem) {
    // Same read/close effect as onOpenChange(false), inlined: routing this
    // through onOpenChange (which also refreshes `now` via Date.now()) from
    // inside the per-item .map() closure defeats the purity checker's static
    // analysis of that impure call's reachability.
    markAllRead()
    setOpen(false)
    const state: OpenCallNavState = { openCallId: item.callId }
    void navigate("/", { state })
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Notifications" className="relative">
          <Bell className="size-5" />
          {unread > 0 && (
            <span
              className="absolute right-0.5 top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-semibold leading-none text-white"
              aria-label={`${unread} unread notifications`}
            >
              {unread > 9 ? "9+" : unread}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent>
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-sm font-semibold">Notifications</span>
          {items.length > 0 && (
            <button
              type="button"
              onClick={clearAll}
              className="text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              Clear
            </button>
          )}
        </div>
        {items.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-3 py-8 text-muted-foreground">
            <BellOff className="size-6 opacity-40" />
            <span className="text-sm">No notifications</span>
          </div>
        ) : (
          <ul className="max-h-80 overflow-y-auto py-1">
            {items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  onClick={() => openCall(item)}
                  className="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-muted"
                >
                  <span
                    className={cn(
                      "mt-1.5 size-2 shrink-0 rounded-full",
                      isUnread(item, cursor) ? "bg-red-500" : "bg-transparent",
                    )}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium">Call needs intervention</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {FLAG_LABELS[item.flag] ?? item.flag} — health {item.score}% ·{" "}
                      {shortCallRef(item.callId)}
                    </span>
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {timeAgo(item.ts, now)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  )
}
