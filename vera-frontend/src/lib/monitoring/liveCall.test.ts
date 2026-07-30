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
    ...overrides,
  }
}

describe("toLiveCall", () => {
  it("maps the form completion percentage into formProgress", () => {
    const live = toLiveCall(callSummary({ completion_pct: 78 }), Date.parse("2026-07-29T00:01:00Z"))
    expect(live.formProgress).toBe(78)
  })

  it("falls back to 0 when the form has no completion projection", () => {
    const live = toLiveCall(callSummary({ completion_pct: null }), Date.parse("2026-07-29T00:01:00Z"))
    expect(live.formProgress).toBe(0)
  })
})
