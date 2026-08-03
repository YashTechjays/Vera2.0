import { describe, expect, it } from "vitest"

import { transcriptText, turnLabel } from "./transcriptText"

describe("turnLabel", () => {
  it("names each source, with the intervener label for supervisor turns", () => {
    expect(turnLabel("bot", "Supervisor")).toBe("Vera")
    expect(turnLabel("rep", "Supervisor")).toBe("Rep")
    expect(turnLabel("supervisor", "sam@x.com")).toBe("sam@x.com")
  })
})

describe("transcriptText", () => {
  it("renders one line per turn, keypad presses as actions", () => {
    const text = transcriptText([
      { role: "agent", source: "bot", text: "Hi there.", ts: 1, supervisorLabel: "Supervisor" },
      { role: "user", source: "rep", text: "Member ID please?", ts: 2, supervisorLabel: "Supervisor" },
      { role: "dtmf", source: "bot", text: "1", ts: 3, supervisorLabel: "Supervisor" },
      { role: "agent", source: "supervisor", text: "Taking over.", ts: 4, supervisorLabel: "sam@x.com" },
    ])
    expect(text).toBe(
      "Vera: Hi there.\nRep: Member ID please?\nVera pressed 1 on the keypad\nsam@x.com: Taking over.",
    )
  })

  it("is empty for no turns", () => {
    expect(transcriptText([])).toBe("")
  })

  it("marks coaching and whisper notes distinctly (never heard on the call)", () => {
    const text = transcriptText([
      {
        role: "coaching",
        source: "supervisor",
        text: "ask about the deductible",
        ts: 1,
        supervisorLabel: "sam@x.com",
      },
      {
        role: "whisper",
        source: "supervisor",
        text: "mention the copay",
        ts: 2,
        supervisorLabel: "sam@x.com",
      },
    ])
    expect(text).toBe(
      "sam@x.com (coaching): ask about the deductible\nsam@x.com (coaching): mention the copay",
    )
  })
})
