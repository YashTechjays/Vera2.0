"""Worker→control-plane event bus over Redis Streams + a consumer group.

The agent worker is DB-less; this is its first-class channel to signal domain
events (call failures, the answered/ended call-status transitions that drive
the consumer's closeout, the answers extracted from the live call by the
Observer runtime, the IVR navigator's handoff signal to a human, and the
call-health observer's periodic assessments) to the control plane.

Most events are PHI-free by construction: only a room_name (tenant+call UUIDs),
an enum, and a timestamp. Two exceptions carry PHI and rely on the stream
being in-boundary Redis (BAA-covered, CMEK at rest) under the SAME posture as
``vera:transcript:*`` — never logged, never echoed by any handler:
``CallAnswerRecordedEvent``, whose extracted answer value is tokenized by
contract and raw today under passthrough; and ``CallHealthEvent``, whose
``reason`` sentence is derived from the conversation. Adding any further
value-bearing event is a compliance decision, not a routine change.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from vera_core.events.stream_bus import StreamBus

WORKER_EVENTS_STREAM = "vera:worker-events"
WORKER_EVENTS_GROUP = "control-plane"
_EVENT_FIELD = "event"


class CallFailureReason(StrEnum):
    """Why an outbound call did not connect (classified from the SIP disconnect)."""

    NO_ANSWER = "no_answer"  # USER_UNAVAILABLE, or the worker's ring timeout
    BUSY_OR_DECLINED = "busy_or_declined"  # USER_REJECTED
    FAILED = "failed"  # SIP_TRUNK_FAILURE / any other pre-answer drop


class CallFailedEvent(BaseModel):
    """Emitted by the worker when an outbound call fails before it is answered."""

    type: Literal["call.failed"] = "call.failed"
    room_name: str
    reason: CallFailureReason
    ts: int  # epoch milliseconds


class CallAnsweredEvent(BaseModel):
    """Emitted when the SIP callee answered — the call is live."""

    type: Literal["call.answered"] = "call.answered"
    room_name: str
    ts: int  # epoch milliseconds


class IvrExitedEvent(BaseModel):
    """Emitted when the IVR navigator hands off to the verification agent —
    the navigator reached a live human (VR2-45 IVR Success numerator)."""

    type: Literal["ivr.exited"] = "ivr.exited"
    room_name: str
    ts: int  # epoch milliseconds


class CallEndedEvent(BaseModel):
    """Emitted from the worker's shutdown callback — the session finished after
    the call was live (hangup by either side, or the agent's end_call tool).
    Written AFTER the transcript ended-sentinel, so it doubles as the control
    plane's trigger to persist the tokenized transcript to Postgres."""

    type: Literal["call.ended"] = "call.ended"
    room_name: str
    # Defaulted so pre-flag payloads still parse; the closeout stamps it on the call row.
    terminated_by_flow_rule: bool = False
    ts: int  # epoch milliseconds


class CallRuleTerminatedEvent(BaseModel):
    """Emitted the moment a terminate_call flow rule's directive is accepted, so the
    fact survives a worker crash — call.ended's flag is only the shutdown-path echo.
    `rule_key` is a schema constant, never PHI."""

    type: Literal["call.rule_terminated"] = "call.rule_terminated"
    room_name: str
    rule_key: str
    ts: int  # epoch milliseconds


class CallAnswerRecordedEvent(BaseModel):
    """Emitted by the Observer when it extracts an answer from the live call, so the
    control plane can write the field_answer row (worker stays DB-less). Carries the
    value — see the module docstring's compliance note. ``evidence_seq`` points into
    ``transcript.seq`` for the supporting turn; ``confidence`` is 0-100."""

    type: Literal["call.answer_recorded"] = "call.answer_recorded"
    room_name: str
    field_path: str
    value: str
    confidence: int | None = None
    evidence_seq: int | None = None
    ts: int  # epoch milliseconds


class CallHealthEvent(BaseModel):
    """Emitted by the worker's call-health observer after each assessable
    analysis. `reason` is PHI (see module docstring) — never log it."""

    type: Literal["call.health"] = "call.health"
    room_name: str
    score: int  # 0-100 (clamped at the producer)
    flag: str  # a CallHealthFlag value ("none" = healthy)
    reason: str  # PHI — never log
    # The analyzer's WINDOWED transcript turn count at analysis time — capped by
    # health_max_turns and reset by re-anchoring (spec §4.2) — NOT the call's
    # cumulative total turn count.
    turn_count: int
    ts: int  # analyzed_at, epoch milliseconds — the consumer's idempotency key


type WorkerEvent = (
    CallFailedEvent
    | CallAnsweredEvent
    | IvrExitedEvent
    | CallEndedEvent
    | CallRuleTerminatedEvent
    | CallAnswerRecordedEvent
    | CallHealthEvent
)
_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(
    Annotated[WorkerEvent, Field(discriminator="type")]
)


def parse_worker_event(raw: str) -> WorkerEvent:
    """Deserialize a stream payload into a typed event; raises on invalid/unknown."""
    return _ADAPTER.validate_json(raw)


class WorkerEventBus(StreamBus):
    """XADD publish side (worker) + consumer-group bootstrap. One stream, one group."""

    stream = WORKER_EVENTS_STREAM
    group = WORKER_EVENTS_GROUP
    payload_field = _EVENT_FIELD

    async def emit(self, event: WorkerEvent) -> None:
        await self._emit_raw(event.model_dump_json())
