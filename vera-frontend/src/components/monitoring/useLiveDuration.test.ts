import { renderHook } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { useLiveDuration } from "./useLiveDuration"

// VR2-213: a completed call must show its real duration, not time-since-start.
describe("useLiveDuration", () => {
  it("freezes the label at the call's end time once ended", () => {
    const { result } = renderHook(() =>
      useLiveDuration({
        open: true,
        ended: true,
        sseMs: null,
        startedAt: "2026-08-19T00:00:00Z",
        endedMs: Date.parse("2026-08-19T00:02:30Z"),
      }),
    )
    expect(result.current.label).toBe("02:30")
    expect(result.current.running).toBe(true)
  })

  it("still measures against the clock while the call is live", () => {
    const startedAt = new Date(Date.now() - 90_000).toISOString()
    const { result } = renderHook(() =>
      useLiveDuration({ open: true, ended: false, sseMs: null, startedAt, endedMs: null }),
    )
    expect(result.current.label).toBe("01:30")
  })
})
