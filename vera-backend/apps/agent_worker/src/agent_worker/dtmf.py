"""DTMF (keypad tone) sending, isolated from the agent/tool plumbing for testability.

The IVR navigator presses phone-menu digits over the LiveKit SIP call:
`LocalParticipant.publish_dtmf` emits a SIP DTMF message that the livekit-sip service
relays to the PSTN leg. Kept DB/agent-free so it unit-tests against a fake participant.
"""

import asyncio
import logging

from livekit import rtc

logger = logging.getLogger("agent_worker")

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


class DtmfTransportError(Exception):
    """Publishing a DTMF tone over the live call failed at the SIP/transport layer.

    Wraps the underlying SDK error so callers handle one DTMF-owned type and never
    reach into livekit's internals."""


async def send_dtmf(participant: rtc.LocalParticipant, digits: str, *, gap_s: float = 0.15) -> str:
    """Send each character of `digits` as a SIP DTMF tone, in order, with a short gap.
    Returns the normalized sequence actually sent (stripped/uppercased).

    Validates the whole sequence first and raises `InvalidDtmfError` before sending
    anything, so a bad character never emits a partial sequence. A publish failure on
    the live call is wrapped as `DtmfTransportError`."""
    seq = digits.strip().upper()
    if not seq:
        # An empty sequence emits zero tones; reject it so a caller can't mistake the
        # silent no-op for a successful press.
        raise InvalidDtmfError("empty DTMF sequence")
    bad = sorted({c for c in seq if c not in _DTMF_CODE})
    if bad:
        raise InvalidDtmfError(f"unsupported DTMF characters: {bad}")
    n = len(seq)
    # Count only — never log the raw digit sequence (it can be PHI, e.g. a member ID).
    logger.debug("send_dtmf: emitting %d DTMF tone(s)", n)
    for i, c in enumerate(seq):
        try:
            await participant.publish_dtmf(code=_DTMF_CODE[c], digit=c)
        except Exception as exc:
            raise DtmfTransportError(str(exc)) from exc
        if i < n - 1:
            await asyncio.sleep(gap_s)
    return seq
