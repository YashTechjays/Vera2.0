import { useEffect } from "react"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterAll, describe, expect, it, vi } from "vitest"

import type { LiveCall } from "@/lib/mock-data"
import type { RoomStatus } from "@/lib/monitoring/liveCallView"

// Stub the heavy children/providers (LiveKit room, IBV form, permissions) so the
// modal renders in isolation, matching LiveCallModal.scroll.test.tsx.
vi.mock("./CallTranscript", () => ({ CallTranscript: () => null }))
vi.mock("./LiveCallRoom", () => ({
  LiveCallRoom: ({ onStatus }: { onStatus?: (status: RoomStatus) => void }) => {
    // Mirrors the real room reporting itself live, so Intervene/Join enable immediately.
    useEffect(() => {
      onStatus?.({ phase: "live", otherIntervener: false, intervenerLabel: null })
    }, [onStatus])
    return null
  },
}))
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

// BROWSER_CALLEE reads import.meta.env at module load, so the flag must be stubbed
// before LiveCallModal is first imported — hence the dynamic import below.
vi.stubEnv("VITE_BROWSER_CALLEE_TRANSPORT", "true")
afterAll(() => vi.unstubAllEnvs())
const { LiveCallModal } = await import("./LiveCallModal")

const activeCall: LiveCall = {
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
}

const noop = () => {}

describe("LiveCallModal Close button", () => {
  it("shows Close in callee mode — the test transport has no takeover to close-lock on", async () => {
    render(<LiveCallModal call={activeCall} open onOpenChange={noop} onExpand={noop} />)

    const joinButton = await screen.findByRole("button", { name: "Join as payer rep" })
    fireEvent.click(joinButton)

    await waitFor(() => expect(screen.getByText("Close")).toBeTruthy())
  })

  it("hides Close while intervening", async () => {
    render(<LiveCallModal call={activeCall} open onOpenChange={noop} onExpand={noop} />)
    // Baseline: listen mode shows Close, same as before this fix.
    expect(screen.getByText("Close")).toBeTruthy()

    const interveneButton = await screen.findByRole("button", { name: "Intervene" })
    fireEvent.click(interveneButton)

    await waitFor(() => expect(screen.queryByText("Close")).toBeNull())
  })
})
