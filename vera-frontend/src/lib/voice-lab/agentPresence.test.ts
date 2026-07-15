import { describe, expect, it } from "vitest"

import { hasAgentParticipant } from "./agentPresence"

// Minimal mock of the Participant shape we care about.
// livekit-client's Participant exposes `.isLocal` and `.isAgent` as booleans.
function makeParticipant(overrides: {
  isLocal?: boolean
  isAgent?: boolean
  identity?: string
}): { isLocal: boolean; isAgent: boolean; identity: string } {
  return {
    isLocal: false,
    isAgent: false,
    identity: "participant-1",
    ...overrides,
  }
}

describe("hasAgentParticipant", () => {
  it("returns false for an empty participant list", () => {
    expect(hasAgentParticipant([])).toBe(false)
  })

  it("returns false when only the local participant is present", () => {
    const local = makeParticipant({ isLocal: true, isAgent: false })
    expect(hasAgentParticipant([local])).toBe(false)
  })

  it("returns false when only a non-agent remote participant is present", () => {
    const remote = makeParticipant({ isLocal: false, isAgent: false, identity: "monitor-abc" })
    expect(hasAgentParticipant([remote])).toBe(false)
  })

  it("returns true when a remote agent participant is present", () => {
    const agent = makeParticipant({ isLocal: false, isAgent: true, identity: "vera-agent" })
    expect(hasAgentParticipant([agent])).toBe(true)
  })

  it("returns true when an agent is mixed with local and non-agent participants", () => {
    const local = makeParticipant({ isLocal: true, isAgent: false })
    const monitor = makeParticipant({ isLocal: false, isAgent: false, identity: "monitor-xyz" })
    const agent = makeParticipant({ isLocal: false, isAgent: true, identity: "vera-agent" })
    expect(hasAgentParticipant([local, monitor, agent])).toBe(true)
  })

  it("ignores the local participant even if it somehow has isAgent=true", () => {
    // The local participant is the browser user — never the AI agent.
    const local = makeParticipant({ isLocal: true, isAgent: true })
    expect(hasAgentParticipant([local])).toBe(false)
  })
})
