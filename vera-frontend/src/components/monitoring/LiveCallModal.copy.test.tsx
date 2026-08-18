import { useEffect } from "react"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import type { LiveCall } from "@/lib/mock-data"

const copyText = vi.fn<(text: string) => Promise<boolean>>(() => Promise.resolve(true))
vi.mock("@/lib/clipboard", () => ({
  copyText: (text: string) => copyText(text),
}))

// Children report their plain text up exactly like the real components do.
vi.mock("./CallTranscript", () => ({
  CallTranscript: ({ onTextChange }: { onTextChange: (text: string) => void }) => {
    useEffect(() => {
      onTextChange("Vera: transcript line")
    }, [onTextChange])
    return null
  },
}))
vi.mock("./CallSummaryPanel", () => ({
  CallSummaryPanel: ({ onTextChange }: { onTextChange: (text: string) => void }) => {
    useEffect(() => {
      onTextChange("Purpose: summary text")
    }, [onTextChange])
    return null
  },
}))
vi.mock("./LiveCallRoom", () => ({ LiveCallRoom: () => null }))
vi.mock("./CoachingPanel", () => ({ CoachingPanel: () => null }))
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

function makeCall(): LiveCall {
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
  }
}

const noop = () => {}

describe("LiveCallModal copy button", () => {
  it("copies the transcript on the Transcription tab", async () => {
    const user = userEvent.setup()
    render(<LiveCallModal call={makeCall()} open onOpenChange={noop} onExpand={noop} />)

    await user.click(screen.getByRole("button", { name: "Copy transcript" }))
    expect(copyText).toHaveBeenCalledWith("Vera: transcript line")
  })

  it("copies the summary on the Summary tab", async () => {
    const user = userEvent.setup()
    render(<LiveCallModal call={makeCall()} open onOpenChange={noop} onExpand={noop} />)

    await user.click(screen.getByRole("button", { name: "Summary" }))
    await user.click(screen.getByRole("button", { name: "Copy summary" }))
    expect(copyText).toHaveBeenCalledWith("Purpose: summary text")
  })

  it("disables copy until the summary has loaded", async () => {
    copyText.mockClear()
    const user = userEvent.setup()
    render(<LiveCallModal call={makeCall()} open onOpenChange={noop} onExpand={noop} />)

    // The panel only mounts (and reports text) once the Summary tab is opened.
    expect(screen.getByRole("button", { name: "Copy transcript" })).toBeEnabled()
    await user.click(screen.getByRole("button", { name: "Summary" }))
    expect(screen.getByRole("button", { name: "Copy summary" })).toBeEnabled()
  })
})
