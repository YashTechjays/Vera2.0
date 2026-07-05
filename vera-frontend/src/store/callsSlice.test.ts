import { describe, expect, it, vi } from "vitest"

// The slice imports the real api client (for `ApiError`), which reads sessionStorage
// at module load — undefined in the node test env. Stub storage like authSlice.test.ts.
vi.mock("@/lib/auth/storage", () => ({
  getToken: () => null,
  setSession: vi.fn(),
  clearSession: vi.fn(),
}))

import type { CallSummary } from "@/lib/api/calls"
import reducer, { fetchCalls, publishCall, selectActiveCalls } from "./callsSlice"

const call = (over: Partial<CallSummary> = {}): CallSummary => ({
  id: "c1",
  tenant_id: "t1",
  status: "active",
  room_name: "call--t1--c1",
  patient_name: null,
  started_at: null,
  created_at: "2026-07-04T00:00:00Z",
  published: false,
  is_owner: true,
  ...over,
})

const initial = reducer(undefined, { type: "@@INIT" })

describe("callsSlice", () => {
  it("starts empty and idle", () => {
    expect(initial.items).toEqual([])
    expect(initial.loading).toBe(false)
    expect(initial.error).toBeNull()
  })

  it("marks loading on fetch pending", () => {
    const s = reducer(initial, fetchCalls.pending("", undefined))
    expect(s.loading).toBe(true)
    expect(s.error).toBeNull()
  })

  it("stores the list on fetch fulfilled", () => {
    const items = [call({ id: "c1" }), call({ id: "c2" })]
    const s = reducer(initial, fetchCalls.fulfilled(items, "", undefined))
    expect(s.loading).toBe(false)
    expect(selectActiveCalls({ calls: s })).toEqual(items)
  })

  it("records an error on fetch rejected", () => {
    const s = reducer(initial, fetchCalls.rejected(new Error("boom"), "", undefined))
    expect(s.loading).toBe(false)
    expect(s.error).toBe("Could not load calls.")
  })

  it("swaps the published call in place on publish fulfilled", () => {
    const loaded = reducer(
      initial,
      fetchCalls.fulfilled([call({ id: "c1", published: false })], "", undefined),
    )
    const s = reducer(loaded, publishCall.fulfilled(call({ id: "c1", published: true }), "", "c1"))
    expect(s.items.find((c) => c.id === "c1")?.published).toBe(true)
  })
})
