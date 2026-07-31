import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

// Stub CallTranscript so the test doesn't open the SSE.
vi.mock("@/components/monitoring/CallTranscript", () => ({
  CallTranscript: ({ callId }: { callId: string }) => <div>transcript:{callId}</div>,
}))

import { TranscriptDialog } from "./TranscriptDialog"

describe("TranscriptDialog", () => {
  it("renders the title and transcript for the given call", () => {
    render(
      <TranscriptDialog
        call={{ id: "c1", patient_name: "Jane Doe", created_at: "2026-07-21T12:00:00Z" }}
        onOpenChange={() => {}}
      />,
    )
    expect(screen.getByText(/Transcript — Jane Doe/)).toBeTruthy()
    expect(screen.getByText("transcript:c1")).toBeTruthy()
  })

  it("renders nothing when call is null (closed)", () => {
    render(<TranscriptDialog call={null} onOpenChange={() => {}} />)
    expect(screen.queryByText(/Transcript —/)).toBeNull()
  })
})
