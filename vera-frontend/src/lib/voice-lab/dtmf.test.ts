import { describe, expect, it } from "vitest"

import { InvalidDtmfError, sendDtmf, type DtmfPublisher } from "./dtmf"

class FakePublisher implements DtmfPublisher {
  sent: Array<[number, string]> = []
  async publishDtmf(code: number, digit: string): Promise<void> {
    this.sent.push([code, digit])
  }
}

describe("sendDtmf", () => {
  it("maps each char to its DTMF code, in order", async () => {
    const p = new FakePublisher()
    await sendDtmf(p, "12*#", { gapMs: 0 })
    expect(p.sent).toEqual([
      [1, "1"],
      [2, "2"],
      [10, "*"],
      [11, "#"],
    ])
  })

  it("trims surrounding whitespace before sending", async () => {
    const p = new FakePublisher()
    await sendDtmf(p, "  9  ", { gapMs: 0 })
    expect(p.sent).toEqual([[9, "9"]])
  })

  it("rejects an unsupported character and sends nothing", async () => {
    const p = new FakePublisher()
    await expect(sendDtmf(p, "12x", { gapMs: 0 })).rejects.toBeInstanceOf(InvalidDtmfError)
    expect(p.sent).toEqual([]) // validated up front — no partial sequence
  })
})
