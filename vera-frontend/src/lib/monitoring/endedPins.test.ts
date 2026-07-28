import { describe, expect, it } from "vitest"

import { applyEndedPins, pinEnded, type EndedPins } from "./endedPins"
import type { CallSummary } from "@/lib/api/calls"

function call(id: string, status = "active"): CallSummary {
  return {
    id,
    tenant_id: "t",
    form_id: "f",
    status,
    room_name: `call--t--${id}`,
    patient_name: null,
    insurance_provider: null,
    insurance_type: null,
    started_at: "2026-07-28T10:00:00Z",
    ended_at: null,
    created_at: "2026-07-28T09:59:00Z",
    published: true,
    is_owner: true,
    health_score: null,
    health_flag: null,
    health_reason: null,
    health_analyzed_at: null,
  } as CallSummary
}

describe("ended-call pins (VR2-72)", () => {
  it("keeps the real terminal status instead of reporting completed", () => {
    const pins: EndedPins = new Map()
    pinEnded(pins, "c1", "no_answer", "2026-07-28T10:01:00Z")
    const [row] = applyEndedPins(pins, [call("c1")])
    expect(row.status).toBe("no_answer")
    expect(row.ended_at).toBe("2026-07-28T10:01:00Z")
  })

  it("keeps the first ended_at when the same status is pinned again", () => {
    const pins: EndedPins = new Map()
    pinEnded(pins, "c1", "failed", "2026-07-28T10:01:00Z")
    pinEnded(pins, "c1", "failed", "2026-07-28T10:02:30Z")
    expect(pins.get("c1")?.ended_at).toBe("2026-07-28T10:01:00Z") // frozen duration
  })

  it("re-applies the pin while the server still lists the call as active", () => {
    const pins: EndedPins = new Map()
    pinEnded(pins, "c1", "canceled", "2026-07-28T10:01:00Z")
    const rows = applyEndedPins(pins, [call("c1"), call("c2")])
    expect(rows.map((r) => r.status)).toEqual(["canceled", "active"])
    expect(pins.has("c1")).toBe(true)
  })

  it("drops the pin once the server stops listing the call", () => {
    const pins: EndedPins = new Map()
    pinEnded(pins, "c1", "completed", "2026-07-28T10:01:00Z")
    applyEndedPins(pins, [call("c2")])
    expect(pins.has("c1")).toBe(false)
  })
})
