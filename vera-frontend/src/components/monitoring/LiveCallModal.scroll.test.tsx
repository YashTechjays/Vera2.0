import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { LiveCall } from "@/lib/mock-data"

// Stub the transcript so we can read the autoScroll prop the modal hands it, and
// stub the heavy children/providers (LiveKit room, IBV form, permissions) so the
// modal renders in isolation.
vi.mock("./CallTranscript", () => ({
  CallTranscript: ({ autoScroll }: { autoScroll?: boolean }) => (
    <div data-testid="transcript">autoScroll={String(autoScroll)}</div>
  ),
}))
vi.mock("./LiveCallRoom", () => ({ LiveCallRoom: () => null }))
vi.mock("./CoachingPanel", () => ({ CoachingPanel: () => null }))
vi.mock("./CallSummaryPanel", () => ({ CallSummaryPanel: () => null }))
vi.mock("./Keypad", () => ({ Keypad: () => null }))
vi.mock("@/components/ibv/SchemaForm", () => ({ SchemaForm: () => null }))
vi.mock("@/components/ibv/IbvProvider", () => ({
  useIbv: () => ({
    applyLiveAnswer: vi.fn(),
    loadFormById: vi.fn(),
    formId: null,
    loading: false,
    error: null,
    schema: null,
  }),
}))
vi.mock("@/lib/auth/permissions", () => ({ usePermission: () => true }))

import { LiveCallModal } from "./LiveCallModal"

function makeCall(overrides: Partial<LiveCall>): LiveCall {
  return {
    id: "c1",
    patient: "Jane Doe",
    type: "Patient",
    agent: "—",
    duration: "0:00",
    status: "completed",
    category: "completed",
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

describe("LiveCallModal transcript auto-scroll", () => {
  it("opens a completed call's transcript at the top (no auto-scroll)", () => {
    render(
      <LiveCallModal
        call={makeCall({ category: "completed" })}
        open
        onOpenChange={noop}
        onExpand={noop}
      />,
    )
    expect(screen.getByTestId("transcript").textContent).toBe("autoScroll=false")
  })

  it("follows a live call by auto-scrolling to the newest turn", () => {
    render(
      <LiveCallModal
        call={makeCall({ status: "active", category: "active" })}
        open
        onOpenChange={noop}
        onExpand={noop}
      />,
    )
    expect(screen.getByTestId("transcript").textContent).toBe("autoScroll=true")
  })
})
