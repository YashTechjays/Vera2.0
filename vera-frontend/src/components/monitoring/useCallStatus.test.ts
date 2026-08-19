import { act, renderHook } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { useCallStatus } from "./useCallStatus"

describe("useCallStatus", () => {
  // VR2-213: the terminal event's ts is what freezes the modal's call timer.
  it("captures the terminal event's timestamp as endedAtMs", () => {
    const { result } = renderHook(() => useCallStatus("c1"))
    act(() => result.current.onCallStatus("active", 1_000))
    expect(result.current.endedAtMs).toBeNull()
    act(() => result.current.onCallStatus("completed", 151_000))
    expect(result.current.callEnded).toBe(true)
    expect(result.current.endedAtMs).toBe(151_000)
  })
})
