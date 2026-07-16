"""Worker→control-plane event bus over Redis Streams + a consumer group.

The agent worker is DB-less; this is its first-class channel to signal domain
events (call failures, and the answered/ended call-status transitions that
drive the consumer's closeout) to the control plane. Events are PHI-free by
construction: only a room_name (tenant+call UUIDs), an enum, and a timestamp —
never a phone number or transcript text.
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


class CallEndedEvent(BaseModel):
    """Emitted from the worker's shutdown callback — the session finished after
    the call was live (hangup by either side, or the agent's end_call tool).
    Written AFTER the transcript ended-sentinel, so it doubles as the control
    plane's trigger to persist the tokenized transcript to Postgres."""

    type: Literal["call.ended"] = "call.ended"
    room_name: str
    ts: int  # epoch milliseconds


type WorkerEvent = CallFailedEvent | CallAnsweredEvent | CallEndedEvent
_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(
    Annotated[
        CallFailedEvent | CallAnsweredEvent | CallEndedEvent,
        Field(discriminator="type"),
    ]
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
