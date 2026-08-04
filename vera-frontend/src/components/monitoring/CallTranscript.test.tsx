import { act, render, screen } from "@testing-library/react"
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest"

import { streamCallEvents, type CallStreamEvent } from "@/lib/api/callEvents"

// Keep the envelope-narrowing helpers real; only stub the SSE connection.
vi.mock("@/lib/api/callEvents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/callEvents")>()
  return { ...actual, streamCallEvents: vi.fn(() => new Promise<void>(() => {})) }
})

import { CallTranscript } from "./CallTranscript"

const scrollIntoView = vi.fn()

beforeAll(() => {
  // jsdom has no scrollIntoView implementation.
  Element.prototype.scrollIntoView = scrollIntoView
})

beforeEach(() => {
  vi.mocked(streamCallEvents).mockClear()
  scrollIntoView.mockClear()
})

function emitTurn(text: string) {
  const opts = vi.mocked(streamCallEvents).mock.calls[0][1]
  const event: CallStreamEvent = {
    type: "transcript",
    data: { role: "agent", source: "bot", text },
    ts: 1,
  }
  act(() => opts.onEvent(event))
}

describe("CallTranscript auto-scroll", () => {
  it("scrolls to the bottom as turns arrive by default", () => {
    render(<CallTranscript callId="c1" />)
    emitTurn("Hello")
    expect(screen.getByText("Hello")).toBeTruthy()
    expect(scrollIntoView).toHaveBeenCalled()
  })

  it("does not scroll when autoScroll is false", () => {
    render(<CallTranscript callId="c1" autoScroll={false} />)
    emitTurn("Hello")
    emitTurn("World")
    expect(screen.getByText("World")).toBeTruthy()
    expect(scrollIntoView).not.toHaveBeenCalled()
  })
})
