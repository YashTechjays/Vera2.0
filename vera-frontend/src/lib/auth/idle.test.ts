import { describe, expect, it } from "vitest"
import { ABSOLUTE_MAX_MS, IDLE_TIMEOUT_MS, computeIdleState } from "@/lib/auth/idle"

const base = { lastActivity: 0, sessionStart: 0 }

describe("computeIdleState", () => {
  it("is active well within the idle window", () => {
    const s = computeIdleState({ ...base, now: 5 * 60 * 1000 })
    expect(s.phase).toBe("active")
  })

  it("enters warning 60s before idle logout", () => {
    const now = IDLE_TIMEOUT_MS - 30 * 1000 // 30s left
    const s = computeIdleState({ ...base, now })
    expect(s.phase).toBe("warning")
    expect(s.secondsLeft).toBe(30)
  })

  it("expires at the idle deadline", () => {
    const s = computeIdleState({ ...base, now: IDLE_TIMEOUT_MS + 1 })
    expect(s.phase).toBe("expired")
    expect(s.secondsLeft).toBe(0)
  })

  it("absolute cap forces logout despite recent activity", () => {
    // Active 1s ago, but the 12h cap is in the past → expired.
    const now = ABSOLUTE_MAX_MS + 1000
    const s = computeIdleState({ now, lastActivity: now - 1000, sessionStart: 0 })
    expect(s.phase).toBe("expired")
  })
})
