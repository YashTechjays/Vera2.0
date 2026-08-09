import { act, render } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { LiveCall } from "@/lib/mock-data"
import { LIVE_CALL_ACTIVITY_EVENT, LIVE_CALL_ACTIVITY_INTERVAL_MS } from "@/lib/auth/idle"

// Same isolation set as LiveCallModal.scroll.test.tsx, plus a captured onCallStatus
// so a test can end the call mid-flight the way the SSE stream would.
let sendCallStatus: ((status: string, ts: number) => void) | undefined
vi.mock("./CallTranscript", () => ({
  CallTranscript: ({ onCallStatus }: { onCallStatus?: (s: string, ts: number) => void }) => {
    sendCallStatus = onCallStatus
    return null
  },
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

const liveCall: LiveCall = {
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

function renderModal(open: boolean) {
  render(<LiveCallModal call={liveCall} open={open} onOpenChange={noop} onExpand={noop} />)
}

let beats = 0
const onBeat = () => {
  beats += 1
}

describe("LiveCallModal live-call activity beacon", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    beats = 0
    sendCallStatus = undefined
    window.addEventListener(LIVE_CALL_ACTIVITY_EVENT, onBeat)
  })
  afterEach(() => {
    window.removeEventListener(LIVE_CALL_ACTIVITY_EVENT, onBeat)
    vi.useRealTimers()
  })

  it("beats while a live call is on screen, with no LiveKit room connection at all", () => {
    // LiveCallRoom is stubbed out — there is no room, so connection state can't
    // be what keeps the session alive here.
    renderModal(true)
    act(() => {
      vi.advanceTimersByTime(2 * LIVE_CALL_ACTIVITY_INTERVAL_MS)
    })

    expect(beats).toBeGreaterThanOrEqual(2)
  })

  it("stops beating once the events stream reports the call ended", () => {
    renderModal(true)
    act(() => {
      sendCallStatus?.("completed", Date.now())
    })

    beats = 0
    act(() => {
      vi.advanceTimersByTime(2 * LIVE_CALL_ACTIVITY_INTERVAL_MS)
    })

    expect(beats).toBe(0)
  })

  it("does not beat while the modal is closed", () => {
    renderModal(false)

    beats = 0
    act(() => {
      vi.advanceTimersByTime(2 * LIVE_CALL_ACTIVITY_INTERVAL_MS)
    })

    expect(beats).toBe(0)
  })
})
