import { describe, expect, it } from "vitest"

import {
  CONNECTION_PHASE_LABEL,
  connectionPhase,
  isWaitingForCall,
  participantModeLabel,
  speakerButtonState,
} from "@/lib/monitoring/liveCallView"

describe("connectionPhase", () => {
  it("maps connected to live", () => {
    expect(connectionPhase("connected", false)).toBe("live")
    expect(connectionPhase("connected", true)).toBe("live")
  })

  it("maps both reconnecting states to reconnecting", () => {
    expect(connectionPhase("reconnecting", true)).toBe("reconnecting")
    expect(connectionPhase("signalReconnecting", true)).toBe("reconnecting")
  })

  it("treats disconnected before any connection as the connect in flight", () => {
    expect(connectionPhase("disconnected", false)).toBe("connecting")
    expect(connectionPhase("connecting", false)).toBe("connecting")
  })

  it("treats disconnected after a connection as the call ending", () => {
    expect(connectionPhase("disconnected", true)).toBe("ended")
  })

  it("has a label for every phase", () => {
    expect(CONNECTION_PHASE_LABEL.connecting).toBe("Connecting…")
    expect(CONNECTION_PHASE_LABEL.live).toBe("Live")
    expect(CONNECTION_PHASE_LABEL.reconnecting).toBe("Reconnecting…")
    expect(CONNECTION_PHASE_LABEL.ended).toBe("Call ended")
  })
})

describe("isWaitingForCall", () => {
  it("waits only while connected with no remote participants", () => {
    expect(isWaitingForCall("connected", 0)).toBe(true)
    expect(isWaitingForCall("connected", 1)).toBe(false)
    expect(isWaitingForCall("connecting", 0)).toBe(false)
    expect(isWaitingForCall("disconnected", 0)).toBe(false)
  })
})

describe("participantModeLabel", () => {
  it("labels publishers as speakers", () => {
    expect(participantModeLabel(true)).toBe("Can speak")
  })

  it("labels non-publishers and unknown permissions as listening", () => {
    expect(participantModeLabel(false)).toBe("Listening")
    expect(participantModeLabel(undefined)).toBe("Listening")
  })
})

describe("speakerButtonState", () => {
  it("offers the autoplay unlock until the browser allows playback", () => {
    expect(speakerButtonState(false, false)).toEqual({
      action: "unlock",
      title: "Enable audio",
      slashed: true,
    })
    // Unlock wins even if the user had muted earlier.
    expect(speakerButtonState(false, true).action).toBe("unlock")
  })

  it("toggles mute once playback is allowed", () => {
    expect(speakerButtonState(true, false)).toEqual({
      action: "mute",
      title: "Mute audio",
      slashed: false,
    })
    expect(speakerButtonState(true, true)).toEqual({
      action: "unmute",
      title: "Unmute audio",
      slashed: true,
    })
  })
})
