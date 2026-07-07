"""Unit tests for the worker's wait_for_speaker greeting-gate.

The agent must hold its greeting until a participant who can actually hear it is
*ready*: the browser caller (ready as soon as it joins) or the SIP callee once it
has **answered** (sip.callStatus == "active"). It must ignore the listen-only
monitor, and must NOT greet into a SIP call that is still ringing.

wait_for_speaker classifies terminal states into a SpeakerReady | CallFailed result:
a SIP callee that drops before answering (busy/declined/trunk error) or a timeout
(treated as no-answer) both produce CallFailed with the appropriate reason.
"""

import asyncio
from typing import Any

import pytest
from livekit import rtc

from agent_worker.main import (
    CallFailed,
    SpeakerReady,
    _is_ready_speaker,
    classify_sip_disconnect,
    wait_for_speaker,
)
from vera_core.events import CallFailureReason

_SIP = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
_STANDARD = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD


class _FakeParticipant:
    def __init__(
        self,
        identity: str,
        *,
        kind: int = _STANDARD,
        attributes: dict[str, str] | None = None,
        disconnect_reason: int | None = None,
    ):
        self.identity = identity
        self.kind = kind
        self.attributes = attributes or {}
        self.disconnect_reason = disconnect_reason


class _FakeRoom:
    def __init__(self, *participants: _FakeParticipant, name: str = "call--t--c") -> None:
        self.name = name
        self.remote_participants = {p.identity: p for p in participants}
        self._handlers: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> Any:
        self._handlers.setdefault(event, []).append(callback)
        return callback

    def off(self, event: str, callback: Any) -> None:
        self._handlers.get(event, []).remove(callback)

    def emit_connected(self, participant: _FakeParticipant) -> None:
        # A connected participant is, by definition, in the room's participant set.
        self.remote_participants[participant.identity] = participant
        for cb in list(self._handlers.get("participant_connected", [])):
            cb(participant)

    def emit_attributes_changed(self, participant: _FakeParticipant) -> None:
        for cb in list(self._handlers.get("participant_attributes_changed", [])):
            cb({}, participant)

    def emit_disconnected(self, participant: _FakeParticipant) -> None:
        self.remote_participants.pop(participant.identity, None)
        for cb in list(self._handlers.get("participant_disconnected", [])):
            cb(participant)


class _FakeCtx:
    def __init__(self, room: _FakeRoom) -> None:
        self.room = room


def _active_sip(identity: str = "phone-callee") -> _FakeParticipant:
    return _FakeParticipant(identity, kind=_SIP, attributes={"sip.callStatus": "active"})


def _ringing_sip(identity: str = "phone-callee") -> _FakeParticipant:
    return _FakeParticipant(identity, kind=_SIP, attributes={"sip.callStatus": "ringing"})


def test_is_ready_speaker_classifies_participants() -> None:
    assert _is_ready_speaker(_FakeParticipant("caller-123")) is True  # type: ignore[arg-type]
    assert _is_ready_speaker(_active_sip()) is True  # type: ignore[arg-type]
    # A SIP callee that is merely ringing (or has no status yet) is NOT ready.
    assert _is_ready_speaker(_ringing_sip()) is False  # type: ignore[arg-type]
    assert _is_ready_speaker(_FakeParticipant("phone-callee", kind=_SIP)) is False  # type: ignore[arg-type]
    # The listen-only monitor never qualifies.
    assert _is_ready_speaker(_FakeParticipant("monitor-123")) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_returns_the_caller_already_present() -> None:
    caller = _FakeParticipant("caller-1")
    ctx = _FakeCtx(_FakeRoom(caller))
    result = await wait_for_speaker(ctx, timeout_s=5.0)  # type: ignore[arg-type]
    assert result == SpeakerReady(caller)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_returns_the_browser_caller_when_it_joins() -> None:
    room = _FakeRoom()
    ctx = _FakeCtx(room)
    caller = _FakeParticipant("caller-1")

    async def _emit() -> None:
        await asyncio.sleep(0)
        room.emit_connected(caller)

    task = asyncio.create_task(_emit())
    result = await wait_for_speaker(ctx, timeout_s=2.0)  # type: ignore[arg-type]
    assert result == SpeakerReady(caller)  # type: ignore[arg-type]
    await task


@pytest.mark.asyncio
async def test_returns_sip_callee_only_after_answer_not_ring() -> None:
    # SIP callee joins while ringing — must NOT unblock — then answers
    # (sip.callStatus → active via an attributes-changed event) — now it unblocks
    # and the answered callee is returned (so the agent links its input to it).
    room = _FakeRoom()
    ctx = _FakeCtx(room)
    callee = _ringing_sip()

    async def _ring_then_answer() -> None:
        await asyncio.sleep(0)
        room.emit_connected(callee)  # ringing — should not satisfy the gate
        await asyncio.sleep(0)
        callee.attributes["sip.callStatus"] = "active"  # callee answers
        room.emit_attributes_changed(callee)

    task = asyncio.create_task(_ring_then_answer())
    result = await wait_for_speaker(ctx, timeout_s=2.0)  # type: ignore[arg-type]
    assert result == SpeakerReady(callee)  # type: ignore[arg-type]
    await task


@pytest.mark.asyncio
async def test_no_answer_when_sip_never_answers() -> None:
    room = _FakeRoom()
    ctx = _FakeCtx(room)

    async def _ring_only() -> None:
        await asyncio.sleep(0)
        room.emit_connected(_ringing_sip())  # rings, never answers

    task = asyncio.create_task(_ring_only())
    result = await wait_for_speaker(ctx, timeout_s=0.05)  # type: ignore[arg-type]
    assert result == CallFailed(CallFailureReason.NO_ANSWER)
    await task


@pytest.mark.asyncio
async def test_no_answer_when_only_monitor_present() -> None:
    room = _FakeRoom(_FakeParticipant("monitor-1"))
    ctx = _FakeCtx(room)
    result = await wait_for_speaker(ctx, timeout_s=0.05)  # type: ignore[arg-type]
    assert result == CallFailed(CallFailureReason.NO_ANSWER)


@pytest.mark.asyncio
async def test_no_answer_when_no_one_joins() -> None:
    ctx = _FakeCtx(_FakeRoom())
    result = await wait_for_speaker(ctx, timeout_s=0.05)  # type: ignore[arg-type]
    assert result == CallFailed(CallFailureReason.NO_ANSWER)


def test_classify_sip_disconnect_maps_reasons() -> None:
    assert classify_sip_disconnect(rtc.DisconnectReason.USER_REJECTED) == (
        CallFailureReason.BUSY_OR_DECLINED
    )
    assert classify_sip_disconnect(rtc.DisconnectReason.USER_UNAVAILABLE) == (
        CallFailureReason.NO_ANSWER
    )
    assert classify_sip_disconnect(rtc.DisconnectReason.SIP_TRUNK_FAILURE) == (
        CallFailureReason.FAILED
    )
    assert classify_sip_disconnect(None) == CallFailureReason.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (rtc.DisconnectReason.USER_REJECTED, CallFailureReason.BUSY_OR_DECLINED),
        (rtc.DisconnectReason.USER_UNAVAILABLE, CallFailureReason.NO_ANSWER),
        (rtc.DisconnectReason.SIP_TRUNK_FAILURE, CallFailureReason.FAILED),
    ],
)
async def test_sip_disconnect_before_answer_fails(reason: int, expected: CallFailureReason) -> None:
    room = _FakeRoom()
    ctx = _FakeCtx(room)
    callee = _ringing_sip()

    async def _ring_then_drop() -> None:
        await asyncio.sleep(0)
        room.emit_connected(callee)
        await asyncio.sleep(0)
        callee.disconnect_reason = reason
        room.emit_disconnected(callee)

    task = asyncio.create_task(_ring_then_drop())
    result = await wait_for_speaker(ctx, timeout_s=2.0)  # type: ignore[arg-type]
    assert result == CallFailed(expected)
    await task
