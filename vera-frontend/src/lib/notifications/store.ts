// Pure state helpers for the notification center (no React — unit-tested).
//
// PHI hygiene: notification ITEMS (call id, flag, score) live in component
// memory only and vanish with the tab. The ONLY thing persisted is the read
// CURSOR — an opaque Redis stream entry id ("<epoch-ms>-<seq>"), which carries
// no PHI. The cursor is what makes reconnect/replay idempotent for the user:
// anything at or below it never re-toasts and never counts as unread, even
// across a page reload; only notifications that arrived while the page was
// down (and were never read) resurface.

export type NotificationItem = {
  /** SSE entry id (Redis stream id) — unique, time-ordered; dedupe/read key. */
  id: string
  callId: string
  flag: string
  score: number
  /** Server publish time, epoch ms. */
  ts: number
}

/** React Router navigation `state` shape used to jump straight to the flagged
 *  call's modal from a notification (bell item, or the toast's "View" action).
 *  Router state — never the URL/query string — so this never violates the
 *  PHI-never-in-URLs rule; `callId` is an opaque UUID, not PHI on its own. */
export type OpenCallNavState = { openCallId: string }

/** A short, non-PHI fragment of the call's opaque id — enough to tell two
 *  concurrently-flagged calls apart in a toast/bell line without showing any
 *  patient detail (e.g. "#7A2F91B4"). Not a lookup key — only ever a hint.
 *  8 hex chars (32 bits) of a UUIDv7's random tail keeps collisions
 *  vanishingly unlikely even across thousands of concurrently-visible calls. */
export function shortCallRef(callId: string): string {
  const compact = callId.replaceAll("-", "")
  return `#${compact.slice(-8).toUpperCase()}`
}

/** Human labels for the analyzer's intervention categories (toast + bell). */
export const FLAG_LABELS: Record<string, string> = {
  supervisor_requested: "Supervisor requested",
  repeated_questions: "Repeated questions",
  hallucination: "Possible hallucination",
  conversation_loop: "Conversation loop",
  long_silence: "Long silence",
  off_script: "Off script",
  low_confidence: "Low confidence",
  other: "Needs attention",
}

const CURSOR_KEY = "vera.notifications.readCursor"
/** Bell history cap — a glanceable inbox, not an archive. */
export const MAX_ITEMS = 50

/** Chronological compare of stream entry ids ("<ms>-<seq>"). */
export function compareEntryIds(a: string, b: string): number {
  const [ams = 0, aseq = 0] = a.split("-").map(Number)
  const [bms = 0, bseq = 0] = b.split("-").map(Number)
  return ams !== bms ? ams - bms : aseq - bseq
}

/** Unread = newer than the read cursor (no cursor yet = everything unread). */
export function isUnread(item: NotificationItem, cursor: string | null): boolean {
  return cursor === null || compareEntryIds(item.id, cursor) > 0
}

export function unreadCount(items: NotificationItem[], cursor: string | null): number {
  return items.filter((item) => isUnread(item, cursor)).length
}

/** Newest-first insert, deduped by entry id, capped at MAX_ITEMS. */
export function addItem(items: NotificationItem[], item: NotificationItem): NotificationItem[] {
  if (items.some((existing) => existing.id === item.id)) return items
  return [item, ...items].slice(0, MAX_ITEMS)
}

/** The newest entry id in the list (the mark-all-read target); null when empty. */
export function latestEntryId(items: NotificationItem[]): string | null {
  return items.reduce<string | null>(
    (latest, item) => (latest === null || compareEntryIds(item.id, latest) > 0 ? item.id : latest),
    null,
  )
}

/** Compact relative timestamp for the bell list. */
export function timeAgo(ts: number, nowMs: number): string {
  const s = Math.max(0, Math.floor((nowMs - ts) / 1000))
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86_400)}d ago`
}

// sessionStorage survives a reload in the same tab (a fresh tab starts a fresh
// inbox); guarded for privacy modes / non-browser test environments.

export function loadReadCursor(): string | null {
  try {
    return sessionStorage.getItem(CURSOR_KEY)
  } catch {
    return null
  }
}

export function saveReadCursor(id: string): void {
  try {
    sessionStorage.setItem(CURSOR_KEY, id)
  } catch {
    // storage unavailable — read state degrades to per-mount memory
  }
}

/** Wipe the cursor at session end (logout / forced logout) so a shared tab's
 *  next login starts with a clean "everything unread" inbox instead of
 *  inheriting whoever was signed in before. */
export function clearReadCursor(): void {
  try {
    sessionStorage.removeItem(CURSOR_KEY)
  } catch {
    // storage unavailable — nothing to clear
  }
}
