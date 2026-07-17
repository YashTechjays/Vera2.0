import { describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import { asInterventionNeeded, type AppNotification } from "@/lib/api/notifications"

describe("asInterventionNeeded", () => {
  it("narrows an intervention_needed notification", () => {
    const n: AppNotification = {
      type: "intervention_needed",
      data: { call_id: "c-1", score: 30, flag: "conversation_loop", reason: "r" },
      ts: 1,
    }
    expect(asInterventionNeeded(n)).toEqual({
      callId: "c-1",
      score: 30,
      flag: "conversation_loop",
    })
  })

  it("returns null for other types or malformed data", () => {
    expect(asInterventionNeeded({ type: "something_else", data: {}, ts: 1 })).toBeNull()
    expect(
      asInterventionNeeded({ type: "intervention_needed", data: { score: 1 }, ts: 1 }),
    ).toBeNull()
  })
})
