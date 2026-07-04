// Idle auto-logout configuration + a pure deadline calculator. Kept side-effect free
// so it can be unit-tested with an injected `now` (no timers, no DOM).
//
// The idle window and the absolute session deadline are NOT hardcoded here: the backend
// is the single source of truth and ships them via `/auth/me` (see authSlice), so the two
// can never drift from the server's real config. Only UX-tuning constants live here.

export const WARNING_LEAD_MS = 60 * 1000 // warn 60s before logout — a frontend UX lead, not a security boundary
export const KEEPALIVE_THROTTLE_MS = 5 * 60 * 1000 // keepalive at most every 5 min

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
