import { render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

// Stubbed rather than imported: pulling the real SDK into jsdom costs the whole suite
// minutes. Literals are inlined because vi.mock is hoisted above any const.
vi.mock("livekit-client", () => ({
  DisconnectReason: { DUPLICATE_IDENTITY: 2, CLIENT_INITIATED: 1 },
  ConnectionState: { Connected: "connected" },
  ParticipantKind: { AGENT: 3 },
  Track: { Source: { Microphone: "microphone" } },
}))

// vi.hoisted lifts the spy above the hoisted vi.mock below, so the factory and the tests share it.
const { useParticipants } = vi.hoisted(() => ({
  useParticipants: vi.fn(),
}))

vi.mock("@livekit/components-react", () => ({
  LiveKitRoom: (props: { children: React.ReactNode }) => <div>{props.children}</div>,
  RoomAudioRenderer: () => null,
  useAudioPlayback: () => ({ canPlayAudio: true, startAudio: () => {} }),
  useConnectionState: () => "connected",
  useParticipantAttributes: () => ({ attributes: {} }),
  useParticipants,
  useTrackToggle: () => ({ enabled: false, pending: false, toggle: () => {} }),
}))

vi.mock("@/lib/api/calls", () => ({
  getJoinToken: vi.fn(() => Promise.resolve({ url: "ws://fake", token: "t" })),
}))

import { LiveCallRoom } from "./LiveCallRoom"

const agent = {
  sid: "a1",
  identity: "vera-agent",
  name: "",
  kind: 3,
  isLocal: false,
  attributes: {},
}

describe("LiveCallRoom participant roster", () => {
  it("keeps the agent visible — callee mode publishes but does not take over", async () => {
    useParticipants.mockReturnValue([agent])
    render(<LiveCallRoom callId="c1" mode="callee" />)
    await waitFor(() => expect(screen.getByText("Vera Agent")).toBeTruthy())
  })

  it("still hides the agent during a real intervene takeover", async () => {
    useParticipants.mockReturnValue([agent])
    render(<LiveCallRoom callId="c1" mode="intervene" />)
    await waitFor(() => expect(screen.getByText("Live")).toBeTruthy())
    expect(screen.queryByText("Vera Agent")).toBeNull()
    expect(screen.queryByText(/Participants \(/)).toBeNull()
  })
})
