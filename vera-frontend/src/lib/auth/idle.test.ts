import { describe, expect, it } from "vitest"
import { computeIdleState } from "@/lib/auth/idle"

const IDLE = 60 * 60 * 1000 // 60 min idle window (backend-supplied)
const ABS = 10 * 60 * 60 * 1000 // 10h absolute cap (backend-supplied)
// Timeouts are now passed in explicitly (backend-driven), not read from constants.
const base = { lastActivity: 0, idleTimeoutMs: IDLE, absoluteDeadline: ABS }

describe("computeIdleState", () => {
  it("is active well within the idle window", () => {
    const s = computeIdleState({ ...base, now: 5 * 60 * 1000 })
    expect(s.phase).toBe("active")
  })

  it("enters warning 60s before idle logout", () => {
    const now = IDLE - 30 * 1000 // 30s left
    const s = computeIdleState({ ...base, now })
    expect(s.phase).toBe("warning")
    expect(s.secondsLeft).toBe(30)
  })

  it("expires at the idle deadline", () => {
    const s = computeIdleState({ ...base, now: IDLE + 1 })
    expect(s.phase).toBe("expired")
    expect(s.secondsLeft).toBe(0)
  })

  it("absolute cap forces logout despite recent activity", () => {
    // Active 1s ago, but the absolute deadline is in the past → expired.
    const now = ABS + 1000
    const s = computeIdleState({ ...base, now, lastActivity: now - 1000 })
    expect(s.phase).toBe("expired")
  })

  it("warns ahead of the absolute cap even under continuous activity", () => {
    // lastActivity keeps the idle window open, so the absolute cap is the binding
    // deadline — the warning must still fire its lead time before it.
    const now = ABS - 30 * 1000
    const s = computeIdleState({ ...base, now, lastActivity: now })
    expect(s.phase).toBe("warning")
    expect(s.secondsLeft).toBe(30)
  })
})
