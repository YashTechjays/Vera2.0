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

const DUPLICATE_IDENTITY = 2 // mirrors livekit-client's DisconnectReason
const CLIENT_INITIATED = 1

// Captures what LiveCallRoom asks LiveKit to do, and hands the test the disconnect
// callback, so the connect/evict cycle is exercised without a LiveKit server.
const rooms: { connect: boolean; onDisconnected?: (reason?: number) => void }[] = []

vi.mock("@livekit/components-react", () => ({
  LiveKitRoom: (props: {
    connect: boolean
    onDisconnected?: (reason?: number) => void
    children: React.ReactNode
  }) => {
    rooms.push({ connect: props.connect, onDisconnected: props.onDisconnected })
    return <div>{props.children}</div>
  },
  RoomAudioRenderer: () => null,
  useAudioPlayback: () => ({ canPlayAudio: true, startAudio: () => {} }),
  useConnectionState: () => "disconnected",
  useParticipantAttributes: () => ({ attributes: {} }),
  useParticipants: () => [],
  useTrackToggle: () => ({ enabled: false, pending: false, toggle: () => {} }),
}))

// vi.hoisted lifts this alongside the hoisted vi.mock calls above, so the factory
// below can close over a spy the tests can also assert against.
const { getJoinToken } = vi.hoisted(() => ({
  getJoinToken: vi.fn(() => Promise.resolve({ url: "ws://fake", token: "t" })),
}))

vi.mock("@/lib/api/calls", () => ({
  getJoinToken,
}))

import { LiveCallRoom } from "./LiveCallRoom"

const latest = () => rooms[rooms.length - 1]

async function mountAndEvict(reason: number) {
  rooms.length = 0
  render(<LiveCallRoom callId="c1" />)
  await waitFor(() => expect(latest()).toBeTruthy())
  latest().onDisconnected?.(reason)
}

describe("LiveCallRoom, displaced by another tab", () => {
  it("stops connecting once evicted, so it can't take the seat back", async () => {
    // Reconnecting re-presents the identity and evicts the other tab, which reconnects
    // in turn — the seat ping-pongs. `connect` is where that cycle has to stop.
    await mountAndEvict(DUPLICATE_IDENTITY)

    await waitFor(() => expect(latest().connect).toBe(false))
  })

  it("says the call moved rather than that it ended", async () => {
    await mountAndEvict(DUPLICATE_IDENTITY)

    await waitFor(() => expect(screen.getByText("Moved to another tab")).toBeTruthy())
    expect(screen.getByText(/continue there/)).toBeTruthy()
  })

  it("offers no way back — rejoining is what restarts the fight", async () => {
    await mountAndEvict(DUPLICATE_IDENTITY)

    await waitFor(() => expect(screen.getByText("Moved to another tab")).toBeTruthy())
    expect(screen.queryByRole("button", { name: /watch here|rejoin|reconnect/i })).toBeNull()
  })

  it("keeps reconnecting after an ordinary disconnect", async () => {
    // Only a duplicate identity means another window took the seat; a dropped
    // connection must still be retryable or a network blip ends the session.
    await mountAndEvict(CLIENT_INITIATED)

    expect(latest().connect).toBe(true)
    expect(screen.queryByText("Moved to another tab")).toBeNull()
  })
})

describe("LiveCallRoom, join-token mode", () => {
  it("requests a callee token in callee mode", async () => {
    render(<LiveCallRoom callId="c1" mode="callee" />)
    await waitFor(() => expect(getJoinToken).toHaveBeenCalledWith("c1", "callee"))
  })

  it("requests a listen token by default", async () => {
    render(<LiveCallRoom callId="c1" />)
    await waitFor(() => expect(getJoinToken).toHaveBeenCalledWith("c1", "listen"))
  })
})
