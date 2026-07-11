import { describe, expect, it } from "vitest"

import {
  CONNECTION_PHASE_LABEL,
  PARTICIPANT_MODE_BADGE,
  agentJoined,
  connectionPhase,
  interveneButtonState,
  isWaitingForCall,
  otherIntervenerPresent,
  participantLabel,
  participantMode,
  shouldAllowClose,
  speakerButtonState,
  type ParticipantLike,
  type RoomStatus,
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

const supervisor = (over: Partial<ParticipantLike> = {}): ParticipantLike => ({
  identity: "supervisor-u1",
  name: "va@tenant.example",
  attributes: { "vera.mode": "listener" },
  ...over,
})

describe("participantMode", () => {
  it("recognizes the agent by participant kind", () => {
    expect(participantMode({ identity: "whatever", isAgent: true })).toBe("agent")
  })

  it("recognizes the SIP callee by its fixed identity", () => {
    expect(participantMode({ identity: "phone-callee" })).toBe("callee")
  })

  it("reads the supervisor mode from the vera.mode attribute", () => {
    expect(participantMode(supervisor())).toBe("listener")
    expect(participantMode(supervisor({ attributes: { "vera.mode": "intervener" } }))).toBe(
      "intervener",
    )
  })

  it("treats human-prefixed identities without the attribute as listeners", () => {
    expect(participantMode({ identity: "supervisor-u1" })).toBe("listener")
    expect(participantMode({ identity: "monitor-x" })).toBe("listener")
    expect(participantMode({ identity: "caller-x" })).toBe("listener")
  })

  it("falls back to agent for unrecognized identities (kind-less agent worker)", () => {
    expect(participantMode({ identity: "vera-agent-abc" })).toBe("agent")
  })

  it("has a badge for every mode", () => {
    expect(PARTICIPANT_MODE_BADGE.intervener).toBe("Intervening")
    expect(PARTICIPANT_MODE_BADGE.listener).toBe("Listening")
    expect(PARTICIPANT_MODE_BADGE.agent).toBe("AI Agent")
    expect(PARTICIPANT_MODE_BADGE.callee).toBe("Caller")
  })
})

describe("participantLabel", () => {
  it("shows the supervisor's email (token name), falling back to identity", () => {
    expect(participantLabel(supervisor())).toBe("va@tenant.example")
    expect(participantLabel({ identity: "supervisor-u1" })).toBe("supervisor-u1")
  })

  it("names the agent and the callee", () => {
    expect(participantLabel({ identity: "x", isAgent: true })).toBe("Vera Agent")
    expect(participantLabel({ identity: "phone-callee" })).toBe("Caller")
  })
})

describe("otherIntervenerPresent", () => {
  it("true only when a non-local participant is intervening", () => {
    const me = supervisor({ isLocal: true, attributes: { "vera.mode": "intervener" } })
    const other = supervisor({ identity: "supervisor-u2" })
    expect(otherIntervenerPresent([me, other])).toBe(false)
    expect(
      otherIntervenerPresent([
        me,
        supervisor({ identity: "supervisor-u2", attributes: { "vera.mode": "intervener" } }),
      ]),
    ).toBe(true)
  })
})

describe("isWaitingForCall", () => {
  const agent: ParticipantLike = { identity: "x", isAgent: true }

  it("waits only while connected without the agent", () => {
    expect(isWaitingForCall("connected", [supervisor()])).toBe(true)
    expect(isWaitingForCall("connected", [supervisor(), agent])).toBe(false)
    expect(isWaitingForCall("connecting", [])).toBe(false)
    expect(isWaitingForCall("disconnected", [])).toBe(false)
  })

  it("agentJoined backs the waiting check", () => {
    expect(agentJoined([supervisor()])).toBe(false)
    expect(agentJoined([agent])).toBe(true)
  })
})

describe("shouldAllowClose", () => {
  it("always allows opening", () => {
    expect(shouldAllowClose("intervene", false, true)).toBe(true)
  })

  it("blocks closing while intervening until the call has ended", () => {
    expect(shouldAllowClose("intervene", false, false)).toBe(false)
    expect(shouldAllowClose("intervene", true, false)).toBe(true)
  })

  it("lets listeners close freely", () => {
    expect(shouldAllowClose("listen", false, false)).toBe(true)
  })
})

describe("interveneButtonState", () => {
  const live: RoomStatus = { phase: "live", otherIntervener: false }

  it("hides the button without the permission", () => {
    expect(interveneButtonState(false, live).visible).toBe(false)
  })

  it("enables only while live with no other intervener", () => {
    expect(interveneButtonState(true, live)).toEqual({ visible: true, disabled: false })
    expect(interveneButtonState(true, null).disabled).toBe(true)
    expect(interveneButtonState(true, { ...live, phase: "connecting" }).disabled).toBe(true)
    expect(interveneButtonState(true, { ...live, phase: "ended" }).disabled).toBe(true)
  })

  it("disables with a reason while another supervisor is intervening", () => {
    const state = interveneButtonState(true, { ...live, otherIntervener: true })
    expect(state.disabled).toBe(true)
    expect(state.title).toBe("Another supervisor is intervening")
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
