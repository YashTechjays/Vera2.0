"""DTMF (keypad tone) sending, isolated from the agent/tool plumbing for testability.

The IVR navigator presses phone-menu digits over the LiveKit SIP call:
`LocalParticipant.publish_dtmf` emits a SIP DTMF message that the livekit-sip service
relays to the PSTN leg. Kept DB/agent-free so it unit-tests against a fake participant.
"""

import asyncio

from livekit import rtc

# Keypad char -> RFC 4733 DTMF event code: 0-9 -> 0-9, * -> 10, # -> 11, A-D -> 12-15.
_DTMF_CODE: dict[str, int] = {
    **{str(d): d for d in range(10)},
    "*": 10,
    "#": 11,
    "A": 12,
    "B": 13,
    "C": 14,
    "D": 15,
}


class InvalidDtmfError(ValueError):
    """A requested keypad sequence contains an unsupported character."""


async def send_dtmf(participant: rtc.LocalParticipant, digits: str, *, gap_s: float = 0.15) -> None:
    """Send each character of `digits` as a SIP DTMF tone, in order, with a short gap.

    Validates the whole sequence first and raises `InvalidDtmfError` before sending
    anything, so a bad character never emits a partial sequence."""
    seq = digits.strip().upper()
    bad = sorted({c for c in seq if c not in _DTMF_CODE})
    if bad:
        raise InvalidDtmfError(f"unsupported DTMF characters: {bad}")
    for i, c in enumerate(seq):
        await participant.publish_dtmf(code=_DTMF_CODE[c], digit=c)
        if i < len(seq) - 1:
            await asyncio.sleep(gap_s)
