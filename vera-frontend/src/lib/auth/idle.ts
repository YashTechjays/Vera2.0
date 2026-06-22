// Idle auto-logout configuration + a pure deadline calculator. Kept side-effect free
// so it can be unit-tested with an injected `now` (no timers, no DOM).

export const IDLE_TIMEOUT_MS = 60 * 60 * 1000 // 60 min — backend idle TTL
export const WARNING_LEAD_MS = 60 * 1000 // warn 60s before logout
export const KEEPALIVE_THROTTLE_MS = 5 * 60 * 1000 // keepalive at most every 5 min
export const ABSOLUTE_MAX_MS = 12 * 60 * 60 * 1000 // 12h absolute session cap

export type IdlePhase = "active" | "warning" | "expired"
export type IdleState = { phase: IdlePhase; secondsLeft: number; logoutAt: number }

export function computeIdleState(args: {
  now: number
  lastActivity: number
  sessionStart: number
  idleTimeoutMs?: number
  warningLeadMs?: number
  absoluteMaxMs?: number
}): IdleState {
  const idleTimeout = args.idleTimeoutMs ?? IDLE_TIMEOUT_MS
  const warningLead = args.warningLeadMs ?? WARNING_LEAD_MS
  const absoluteMax = args.absoluteMaxMs ?? ABSOLUTE_MAX_MS

  const idleDeadline = args.lastActivity + idleTimeout
  const absoluteDeadline = args.sessionStart + absoluteMax
  // The absolute cap wins even under continuous activity.
  const logoutAt = Math.min(idleDeadline, absoluteDeadline)
  const remaining = logoutAt - args.now
  const secondsLeft = Math.max(0, Math.ceil(remaining / 1000))

  let phase: IdlePhase
  if (remaining <= 0) phase = "expired"
  else if (remaining <= warningLead) phase = "warning"
  else phase = "active"

  return { phase, secondsLeft, logoutAt }
}
