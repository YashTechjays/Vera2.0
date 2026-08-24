import { describe, expect, it } from "vitest"

import type { CallSummary } from "@/lib/api/calls"
import { toLiveCall } from "@/lib/monitoring/liveCall"

function callSummary(overrides: Partial<CallSummary> = {}): CallSummary {
  return {
    id: "c1",
    tenant_id: "t1",
    form_id: "f1",
    status: "active",
    room_name: "call--t1--c1",
    patient_name: null,
    insurance_provider: null,
    insurance_type: null,
    started_at: null,
    ended_at: null,
    created_at: "2026-07-29T00:00:00Z",
    published: false,
    is_owner: true,
    health_score: null,
    health_flag: null,
    health_reason: null,
    health_analyzed_at: null,
    completion_pct: null,
    verified_pct: null,
    ...overrides,
  }
}

describe("toLiveCall", () => {
  // VR2-213: a completed call's timer must be its real duration, not time-since-start.
  it("freezes the duration at ended_at for a completed call", () => {
    const live = toLiveCall(
      callSummary({
        status: "completed",
        started_at: "2026-08-19T00:00:00Z",
        ended_at: "2026-08-19T00:03:24Z",
      }),
      Date.parse("2026-08-19T02:00:00Z"),
    )
    expect(live.duration).toBe("03:24")
    expect(live.callTime).toBe("03:24")
    expect(live.endedAt).toBe("2026-08-19T00:03:24Z")
  })

  it("keeps measuring against now while the call has no ended_at", () => {
    const live = toLiveCall(
      callSummary({ status: "active", started_at: "2026-08-19T00:00:00Z" }),
      Date.parse("2026-08-19T00:01:30Z"),
    )
    expect(live.callTime).toBe("01:30")
    expect(live.endedAt).toBeNull()
  })

  it("maps the form completion percentage into formProgress", () => {
    const live = toLiveCall(callSummary({ completion_pct: 78 }), Date.parse("2026-07-29T00:01:00Z"))
    expect(live.formProgress).toBe(78)
  })

  it("falls back to 0 when the form has no completion projection", () => {
    const live = toLiveCall(callSummary({ completion_pct: null }), Date.parse("2026-07-29T00:01:00Z"))
    expect(live.formProgress).toBe(0)
  })

  it("maps verified_pct into verifiedProgress", () => {
    const live = toLiveCall(callSummary({ verified_pct: 40 }), Date.parse("2026-08-04T00:01:00Z"))
    expect(live.verifiedProgress).toBe(40)
  })

  it("preserves null verified_pct (live/pre-eval renders no Verified label, not 0%)", () => {
    const live = toLiveCall(callSummary({ verified_pct: null }), Date.parse("2026-08-04T00:01:00Z"))
    expect(live.verifiedProgress).toBeNull()
  })
})
