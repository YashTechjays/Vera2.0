import { describe, expect, it } from "vitest"

import { elapsed, startMs } from "./liveTimer"

describe("elapsed", () => {
  it("formats mm:ss since the start", () => {
    const start = Date.parse("2026-07-12T10:00:00Z")
    expect(elapsed(start, start + 5_000)).toBe("00:05")
    expect(elapsed(start, start + 65_000)).toBe("01:05")
    expect(elapsed(start, start + 3_599_000)).toBe("59:59")
  })

  it("accepts an ISO string start", () => {
    const iso = "2026-07-12T10:00:00Z"
    expect(elapsed(iso, Date.parse(iso) + 90_000)).toBe("01:30")
  })

  it("returns a dash until the call has started", () => {
    expect(elapsed(null, Date.now())).toBe("—")
    expect(elapsed(undefined, Date.now())).toBe("—")
  })

  it("clamps a start slightly in the future to zero (clock skew)", () => {
    const now = Date.now()
    expect(elapsed(now + 3_000, now)).toBe("00:00")
  })
})

describe("startMs", () => {
  const iso = "2026-07-12T10:00:00Z"

  it("prefers the SSE-observed start over the polled seed", () => {
    expect(startMs(1_752_314_400_000, iso)).toBe(1_752_314_400_000)
  })

  it("falls back to the polled started_at when SSE has not delivered yet", () => {
    expect(startMs(null, iso)).toBe(Date.parse(iso))
  })

  it("is null while neither source knows the call started", () => {
    expect(startMs(null, null)).toBeNull()
    expect(startMs(null, undefined)).toBeNull()
  })
})
