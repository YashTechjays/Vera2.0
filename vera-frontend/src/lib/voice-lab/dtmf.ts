// Send DTMF (keypad tones) from the browser into the live LiveKit call. The local
// participant publishes SIP DTMF; livekit-sip relays it to the PSTN leg. Kept free of
// React/livekit-client types so it unit-tests against a fake publisher. Mirrors the
// backend agent_worker/dtmf.py (keep the code map in sync).

/** Keypad char -> RFC 4733 DTMF event code. The Keypad UI offers only 0-9, * and #. */
export const DTMF_CODE: Record<string, number> = {
  "0": 0,
  "1": 1,
  "2": 2,
  "3": 3,
  "4": 4,
  "5": 5,
  "6": 6,
  "7": 7,
  "8": 8,
  "9": 9,
  "*": 10,
  "#": 11,
}

export class InvalidDtmfError extends Error {
  constructor(badChars: string[]) {
    super(`unsupported DTMF characters: ${JSON.stringify(badChars)}`)
    this.name = "InvalidDtmfError"
  }
}

/** The slice of LocalParticipant we need — a LocalParticipant satisfies this. */
export interface DtmfPublisher {
  publishDtmf(code: number, digit: string): Promise<void>
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Send each character of `digits` as a SIP DTMF tone, in order, with a short gap.
 * Validates the whole sequence first and throws `InvalidDtmfError` before sending
 * anything, so a bad character never emits a partial sequence.
 */
export async function sendDtmf(
  publisher: DtmfPublisher,
  digits: string,
  { gapMs = 150 }: { gapMs?: number } = {},
): Promise<void> {
  const seq = digits.trim()
  const bad = [...new Set([...seq].filter((c) => !(c in DTMF_CODE)))]
  if (bad.length) throw new InvalidDtmfError(bad)
  for (let i = 0; i < seq.length; i++) {
    const digit = seq[i]
    const code = DTMF_CODE[digit]
    console.debug("[DTMF] publish", { digit, code })
    await publisher.publishDtmf(code, digit)
    if (i < seq.length - 1) await sleep(gapMs)
  }
  console.info("[DTMF] sent %d tone(s): %s", seq.length, seq)
}
