// Idle auto-logout configuration + a pure deadline calculator. Kept side-effect free
// so it can be unit-tested with an injected `now` (no timers, no DOM).
//
// The idle window and the absolute session deadline are NOT hardcoded here: the backend
// is the single source of truth and ships them via `/auth/me` (see authSlice), so the two
// can never drift from the server's real config. Only UX-tuning constants live here.

export const WARNING_LEAD_MS = 60 * 1000 // warn 60s before logout — a frontend UX lead, not a security boundary
export const KEEPALIVE_THROTTLE_MS = 5 * 60 * 1000 // keepalive at most every 5 min

// Synthetic activity dispatched while the live-call modal shows an un-ended call, so
// supervising a call (which needs no mouse/keyboard) never idle-expires the session
// mid-call — even when the audio-room connection drops (VR2-167).
// The interval must stay well under WARNING_LEAD_MS so an open call keeps the
// idle deadline a full window away and the warning never fires. The absolute
// session deadline still applies — this slides only the idle window.
export const LIVE_CALL_ACTIVITY_EVENT = "vera:live-call-activity"
export const LIVE_CALL_ACTIVITY_INTERVAL_MS = 30 * 1000

export type IdlePhase = "active" | "warning" | "expired"
export type IdleState = { phase: IdlePhase; secondsLeft: number; logoutAt: number }

export function computeIdleState(args: {
  now: number
  lastActivity: number
  idleTimeoutMs: number
  absoluteDeadline: number
  warningLeadMs?: number
}): IdleState {
  const warningLead = args.warningLeadMs ?? WARNING_LEAD_MS

  const idleDeadline = args.lastActivity + args.idleTimeoutMs
  // The absolute cap wins even under continuous activity.
  const logoutAt = Math.min(idleDeadline, args.absoluteDeadline)
  const remaining = logoutAt - args.now
  const secondsLeft = Math.max(0, Math.ceil(remaining / 1000))

  let phase: IdlePhase
  if (remaining <= 0) phase = "expired"
  else if (remaining <= warningLead) phase = "warning"
  else phase = "active"

  return { phase, secondsLeft, logoutAt }
}
