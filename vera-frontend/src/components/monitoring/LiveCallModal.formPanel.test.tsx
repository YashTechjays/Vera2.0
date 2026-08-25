import { fireEvent, render, screen } from "@testing-library/react"
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest"

import type { LiveCall } from "@/lib/mock-data"

// Parameterizable IBV context: tests flip the cached form and the dirty flag.
const ibv = vi.hoisted(() => ({
  loadFormById: vi.fn(),
  formId: null as string | null,
  dirty: false,
}))

// Stub the heavy children/providers so the modal renders in isolation
// (same set as LiveCallModal.close.test.tsx).
vi.mock("./CallTranscript", () => ({ CallTranscript: () => null }))
vi.mock("./LiveCallRoom", () => ({ LiveCallRoom: () => null }))
vi.mock("./CoachingPanel", () => ({ CoachingPanel: () => null }))
vi.mock("./CallSummaryPanel", () => ({ CallSummaryPanel: () => null }))
vi.mock("./Keypad", () => ({ Keypad: () => null }))
vi.mock("@/components/ibv/SchemaForm", () => ({ SchemaForm: () => null }))
vi.mock("@/components/ibv/IbvProvider", () => ({
  useIbv: () => ({
    applyLiveAnswer: vi.fn(),
    loadFormById: ibv.loadFormById,
    formId: ibv.formId,
    dirty: ibv.dirty,
    loading: false,
    error: null,
    schema: null,
  }),
}))
vi.mock("@/lib/auth/permissions", () => ({ usePermission: () => true }))

vi.stubEnv("VITE_BROWSER_CALLEE_TRANSPORT", "true")
afterAll(() => vi.unstubAllEnvs())
const { LiveCallModal } = await import("./LiveCallModal")

function call(overrides: Partial<LiveCall>): LiveCall {
  return {
    id: "c1",
    patient: "Jane Doe",
    type: "Patient",
    agent: "—",
    duration: "0:00",
    status: "active",
    category: "active",
    visible: true,
    action: "view",
    insurance: "UHC",
    confidence: 0,
    formProgress: 0,
    verifiedProgress: null,
    formId: "f1",
    callTime: "0:00",
    startedAt: "2026-07-21T12:00:00Z",
    healthScore: 95,
    isOwner: true,
    ...overrides,
  }
}

const noop = () => {}

function expandFormPanel() {
  fireEvent.click(screen.getByText("Patient Information Form"))
}

beforeEach(() => {
  ibv.loadFormById.mockClear()
  ibv.formId = null
  ibv.dirty = false
})

describe("FormPanel refetch on an ended call (VR2-295)", () => {
  it("keeps the cached form while the call is live", () => {
    ibv.formId = "f1" // already loaded during the live watch
    render(<LiveCallModal call={call({})} open onOpenChange={noop} onExpand={noop} />)
    expandFormPanel()
    expect(ibv.loadFormById).not.toHaveBeenCalled()
  })

  it("refetches the cached form once the call has completed — its status went stale", () => {
    ibv.formId = "f1"
    render(
      <LiveCallModal
        call={call({ status: "completed", category: "completed" })}
        open
        onOpenChange={noop}
        onExpand={noop}
      />,
    )
    expandFormPanel()
    expect(ibv.loadFormById).toHaveBeenCalledWith("f1")
  })

  it("never wipes unsaved edits — no refetch while dirty, even after completion", () => {
    ibv.formId = "f1"
    ibv.dirty = true
    render(
      <LiveCallModal
        call={call({ status: "completed", category: "completed" })}
        open
        onOpenChange={noop}
        onExpand={noop}
      />,
    )
    expandFormPanel()
    expect(ibv.loadFormById).not.toHaveBeenCalled()
  })

  it("loads a form it has never seen regardless of call state", () => {
    render(<LiveCallModal call={call({})} open onOpenChange={noop} onExpand={noop} />)
    expandFormPanel()
    expect(ibv.loadFormById).toHaveBeenCalledWith("f1")
  })
})
