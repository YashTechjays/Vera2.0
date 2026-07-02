"""Unit tests for DTMF sending (no live room)."""

import pytest

from agent_worker.dtmf import InvalidDtmfError, send_dtmf


class _FakeParticipant:
    """Records publish_dtmf calls in order; mirrors rtc.LocalParticipant's signature."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.sent.append((code, digit))


@pytest.mark.asyncio
async def test_send_dtmf_maps_codes_in_order() -> None:
    p = _FakeParticipant()
    await send_dtmf(p, "12*#", gap_s=0)  # type: ignore[arg-type]
    assert p.sent == [(1, "1"), (2, "2"), (10, "*"), (11, "#")]


@pytest.mark.asyncio
async def test_send_dtmf_normalizes_case_and_whitespace() -> None:
    p = _FakeParticipant()
    await send_dtmf(p, " a ", gap_s=0)  # type: ignore[arg-type]
    assert p.sent == [(12, "A")]


@pytest.mark.asyncio
async def test_send_dtmf_rejects_bad_char_and_sends_nothing() -> None:
    p = _FakeParticipant()
    with pytest.raises(InvalidDtmfError):
        await send_dtmf(p, "12x", gap_s=0)  # type: ignore[arg-type]
    assert p.sent == []  # validated up front — no partial sequence emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("digits", ["", "   "])
async def test_send_dtmf_rejects_empty_sequence(digits: str) -> None:
    # An empty sequence emits zero tones; reject it so the silent no-op can't be mistaken
    # for a successful press.
    p = _FakeParticipant()
    with pytest.raises(InvalidDtmfError):
        await send_dtmf(p, digits, gap_s=0)  # type: ignore[arg-type]
    assert p.sent == []
