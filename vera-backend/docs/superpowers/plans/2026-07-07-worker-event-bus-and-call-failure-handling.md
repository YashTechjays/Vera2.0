# Worker→Control-Plane Event Bus + Outbound Call-Failure Handling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Voice Lab outbound call fails (busy, declined, no-answer, carrier error), auto-close the LiveKit session and show the browser a specific reason — carried over a new, reusable worker→control-plane event bus.

**Architecture:** The DB-less agent worker detects the SIP callee's terminal `disconnect_reason` and emits a typed `call.failed` event onto a **Redis Streams** stream. A **consumer group** in the control plane delivers each event to exactly one of N replicas; the handler sets LiveKit **room metadata** (the reason) then deletes the room (server-authoritative teardown). The browser reads the metadata via `useRoomInfo()` and renders a proper error banner.

**Tech Stack:** Python 3.12, FastAPI, `redis.asyncio` (Streams + consumer groups), livekit-agents / livekit-api, Pydantic v2, pytest / pytest-asyncio; React + TypeScript + Vite + vitest, `@livekit/components-react`.

**Spec:** `docs/superpowers/specs/2026-07-07-worker-event-bus-and-call-failure-handling-design.md`

## Global Constraints

- **PHI boundary:** events carry only `room_name` (tenant+call UUIDs), a reason enum, and a timestamp — never a phone number or transcript text. Never log raw PHI.
- **Python style:** PEP 695 type params only (`class Foo[T]`); ruff rejects `Generic[T]`/`TypeVar`. `asyncio` is the only async runtime — never `import anyio`.
- **Backend test/gate command:** run from `vera-backend/`: `uv run pytest <path>`; full gate `just check` (ruff + mypy --strict + pytest).
- **Frontend gate:** run from `vera-frontend/`: `npm run test` (`vitest run`), `npm run lint` (`eslint .`), `npm run build` (`tsc -b && vite build`). For a single test file use `npx vitest run <path>`.
- **Idempotent handlers:** every control-plane event handler must be safe to run twice (at-least-once redelivery). `delete_room` already swallows `not_found`; `set_room_metadata` is last-write-wins.
- **Frontend has no DOM test runner** (node-env vitest). Put logic in pure modules with unit tests; React glue is verified by `tsc`/`eslint`/`build`.
- **Commit messages:** no `Co-Authored-By` trailer.

---

### Task 1: Shared event contract + Redis-Streams bus (`vera_core.events`)

**Files:**
- Create: `packages/vera_core/src/vera_core/events/__init__.py`
- Create: `packages/vera_core/src/vera_core/events/worker.py`
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (add fields after line 37, `transcript_end_grace_seconds`)
- Test: `tests/unit/events/test_worker_events.py`

**Interfaces:**
- Produces:
  - `WORKER_EVENTS_STREAM: str`, `WORKER_EVENTS_GROUP: str`
  - `class CallFailureReason(StrEnum)`: `NO_ANSWER="no_answer"`, `BUSY_OR_DECLINED="busy_or_declined"`, `FAILED="failed"`
  - `class CallFailedEvent(BaseModel)`: `type: Literal["call.failed"]`, `room_name: str`, `reason: CallFailureReason`, `ts: int`
  - `WorkerEvent` (type alias, currently `CallFailedEvent`)
  - `def parse_worker_event(raw: str) -> WorkerEvent`
  - `class WorkerEventBus`: `__init__(redis: Redis, *, maxlen: int = 10_000)`, `async emit(event: WorkerEvent) -> None`, `async ensure_group() -> None`
  - `Settings` fields: `worker_events_stream_maxlen: int`, `worker_events_block_ms: int`, `worker_events_reclaim_idle_ms: int`, `call_failed_teardown_grace_ms: int`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/events/test_worker_events.py`:

```python
"""Unit tests for the worker→control-plane event contract and Redis-stream bus."""

import pytest

from vera_core.events import (
    WORKER_EVENTS_GROUP,
    WORKER_EVENTS_STREAM,
    CallFailedEvent,
    CallFailureReason,
    WorkerEventBus,
    parse_worker_event,
)


def test_call_failed_event_round_trips() -> None:
    event = CallFailedEvent(
        room_name="call--t--c", reason=CallFailureReason.BUSY_OR_DECLINED, ts=1720000000000
    )
    parsed = parse_worker_event(event.model_dump_json())
    assert parsed == event
    assert parsed.type == "call.failed"


def test_parse_rejects_unknown_event_type() -> None:
    with pytest.raises(Exception):
        parse_worker_event('{"type": "not.a.real.event", "room_name": "r", "ts": 1}')


def test_event_carries_no_phi_fields() -> None:
    # The wire payload must never grow a phone-number / transcript field.
    assert set(CallFailedEvent.model_fields) == {"type", "room_name", "reason", "ts"}


class _FakeRedis:
    def __init__(self) -> None:
        self.added: list[tuple[str, dict[str, str], int | None, bool]] = []
        self.group_calls: list[tuple[str, str]] = []
        self.busygroup = False

    async def xadd(self, stream, fields, *, maxlen=None, approximate=False):  # noqa: ANN001
        self.added.append((stream, fields, maxlen, approximate))

    async def xgroup_create(self, stream, group, *, id, mkstream):  # noqa: ANN001, A002
        from redis.exceptions import ResponseError

        self.group_calls.append((stream, group))
        if self.busygroup:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")


@pytest.mark.asyncio
async def test_emit_xadds_trimmed_json() -> None:
    redis = _FakeRedis()
    bus = WorkerEventBus(redis, maxlen=500)  # type: ignore[arg-type]
    event = CallFailedEvent(room_name="call--t--c", reason=CallFailureReason.NO_ANSWER, ts=1)
    await bus.emit(event)
    stream, fields, maxlen, approximate = redis.added[0]
    assert stream == WORKER_EVENTS_STREAM
    assert parse_worker_event(fields["event"]) == event
    assert (maxlen, approximate) == (500, True)


@pytest.mark.asyncio
async def test_ensure_group_is_idempotent_on_busygroup() -> None:
    redis = _FakeRedis()
    redis.busygroup = True
    bus = WorkerEventBus(redis)  # type: ignore[arg-type]
    await bus.ensure_group()  # must not raise
    assert redis.group_calls == [(WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/events/test_worker_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vera_core.events'`.

- [ ] **Step 3: Write the event module**

Create `packages/vera_core/src/vera_core/events/worker.py`:

```python
"""Worker→control-plane event bus over Redis Streams + a consumer group.

The agent worker is DB-less; this is its first-class channel to signal domain
events (call failures today; call-status transitions for the real /calls flow
later) to the control plane. Events are PHI-free by construction: only a
room_name (tenant+call UUIDs), an enum, and a timestamp — never a phone number
or transcript text.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, TypeAdapter
from redis.asyncio import Redis
from redis.exceptions import ResponseError

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


# Widen to a discriminated `Union[...]` on `type` when a second event type lands.
type WorkerEvent = CallFailedEvent
_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(CallFailedEvent)


def parse_worker_event(raw: str) -> WorkerEvent:
    """Deserialize a stream payload into a typed event; raises on invalid/unknown."""
    return _ADAPTER.validate_json(raw)


class WorkerEventBus:
    """XADD publish side (worker) + consumer-group bootstrap. One stream, one group."""

    def __init__(self, redis: Redis, *, maxlen: int = 10_000) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def emit(self, event: WorkerEvent) -> None:
        await self._redis.xadd(
            WORKER_EVENTS_STREAM,
            {_EVENT_FIELD: event.model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
```

Create `packages/vera_core/src/vera_core/events/__init__.py`:

```python
"""Worker→control-plane event bus (Redis Streams). See events/worker.py."""

from vera_core.events.worker import (
    WORKER_EVENTS_GROUP,
    WORKER_EVENTS_STREAM,
    CallFailedEvent,
    CallFailureReason,
    WorkerEvent,
    WorkerEventBus,
    parse_worker_event,
)

__all__ = [
    "WORKER_EVENTS_GROUP",
    "WORKER_EVENTS_STREAM",
    "CallFailedEvent",
    "CallFailureReason",
    "WorkerEvent",
    "WorkerEventBus",
    "parse_worker_event",
]
```

- [ ] **Step 4: Add the settings fields**

In `packages/vera_core/src/vera_core/config/settings.py`, immediately after the
`transcript_end_grace_seconds` line (line 37), add:

```python

    # Worker→control-plane event bus (Redis Streams + consumer group). Stream is
    # MAXLEN-trimmed; the consumer blocks for block_ms, reclaims entries a crashed
    # consumer left pending after reclaim_idle_ms, and waits teardown_grace_ms after
    # setting failure metadata before deleting the room (so the browser reads it).
    worker_events_stream_maxlen: int = 10_000  # VERA_WORKER_EVENTS_STREAM_MAXLEN
    worker_events_block_ms: int = 5_000  # VERA_WORKER_EVENTS_BLOCK_MS
    worker_events_reclaim_idle_ms: int = 60_000  # VERA_WORKER_EVENTS_RECLAIM_IDLE_MS
    call_failed_teardown_grace_ms: int = 1_500  # VERA_CALL_FAILED_TEARDOWN_GRACE_MS
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/events/test_worker_events.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/events tests/unit/events \
        packages/vera_core/src/vera_core/config/settings.py
git commit -m "feat(events): worker→control-plane event bus contract + Redis-stream bus"
```

---

### Task 2: LiveKit gateway — `set_room_metadata` (+ fake)

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py` (add method after `delete_room`, ~line 141)
- Modify: `tests/integration/control_plane/conftest.py` (`FakeLiveKit`, ~lines 44-79)
- Test: `tests/integration/control_plane/test_livekit_gateway.py`

**Interfaces:**
- Produces: `LiveKitGateway.set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None`
- Produces (test fake): `FakeLiveKit.room_metadata: list[tuple[str, dict[str, object]]]`, `FakeLiveKit.set_room_metadata(...)`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/control_plane/test_livekit_gateway.py`:

```python
def test_set_room_metadata_serializes_json(monkeypatch) -> None:  # noqa: ANN001
    """set_room_metadata JSON-encodes the dict into an UpdateRoomMetadataRequest."""
    import json

    from livekit import api

    from control_plane.livekit_gateway import LiveKitGateway

    captured: dict[str, object] = {}

    class _FakeRoomService:
        async def update_room_metadata(self, req: api.UpdateRoomMetadataRequest) -> None:
            captured["room"] = req.room
            captured["metadata"] = req.metadata

    class _FakeLkApi:
        room = _FakeRoomService()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(api, "LiveKitAPI", lambda *a, **k: _FakeLkApi())
    gw = LiveKitGateway(url="ws://x", api_key="k", api_secret="s")

    import asyncio

    asyncio.run(gw.set_room_metadata("call--t--c", {"status": "call_failed", "reason": "no_answer"}))
    assert captured["room"] == "call--t--c"
    assert json.loads(captured["metadata"]) == {"status": "call_failed", "reason": "no_answer"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_livekit_gateway.py::test_set_room_metadata_serializes_json -q`
Expected: FAIL — `AttributeError: 'LiveKitGateway' object has no attribute 'set_room_metadata'`.

- [ ] **Step 3: Implement the gateway method**

In `apps/control_plane/src/control_plane/livekit_gateway.py`, add after `delete_room` (before `mint_join_token`):

```python
    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        """Set room-level metadata (JSON-encoded). LiveKit pushes it to every
        participant as a RoomMetadataChanged event, so the browser can read
        session status (e.g. a failed outbound call) before the room is torn down.
        """
        async with self._client() as lk:
            await lk.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(metadata))
            )
```

- [ ] **Step 4: Add the fake recorder**

In `tests/integration/control_plane/conftest.py`, inside `FakeLiveKit.__init__` add (next to `self.deleted`):

```python
        self.room_metadata: list[tuple[str, dict[str, object]]] = []
```

and add the method (next to `delete_room`):

```python
    async def set_room_metadata(
        self, room_name: str, metadata: dict[str, object]
    ) -> None:
        self.room_metadata.append((room_name, metadata))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_livekit_gateway.py -q`
Expected: PASS (existing tests + the new one).

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/livekit_gateway.py \
        tests/integration/control_plane/conftest.py \
        tests/integration/control_plane/test_livekit_gateway.py
git commit -m "feat(livekit): gateway set_room_metadata for room-level session status"
```

---

### Task 3: Worker — classify SIP disconnect, `wait_for_speaker` returns a result

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/main.py` (lines 44-116, plus imports)
- Test: `tests/unit/worker/test_wait_for_speaker.py` (update existing + add cases)

**Interfaces:**
- Consumes: `CallFailureReason` (Task 1)
- Produces:
  - `@dataclass(frozen=True) class SpeakerReady: participant: rtc.RemoteParticipant`
  - `@dataclass(frozen=True) class CallFailed: reason: CallFailureReason`
  - `type WaitResult = SpeakerReady | CallFailed`
  - `def classify_sip_disconnect(reason: int | None) -> CallFailureReason`
  - `async def wait_for_speaker(ctx, timeout_s=...) -> WaitResult` (return type changed from `RemoteParticipant | None`)

- [ ] **Step 1: Update the existing tests + add failure cases**

Replace the body of `tests/unit/worker/test_wait_for_speaker.py` below the imports so it
uses the new result types. Change the import line to:

```python
from agent_worker.main import (
    CallFailed,
    SpeakerReady,
    _is_ready_speaker,
    classify_sip_disconnect,
    wait_for_speaker,
)
from vera_core.events import CallFailureReason
```

Give `_FakeParticipant` a `disconnect_reason`, and add `emit_disconnected` to `_FakeRoom`:

```python
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
```

```python
    def emit_disconnected(self, participant: _FakeParticipant) -> None:
        self.remote_participants.pop(participant.identity, None)
        for cb in list(self._handlers.get("participant_disconnected", [])):
            cb(participant)
```

Update the three "returns X" assertions to unwrap `SpeakerReady`, e.g.:

```python
@pytest.mark.asyncio
async def test_returns_the_caller_already_present() -> None:
    caller = _FakeParticipant("caller-1")
    ctx = _FakeCtx(_FakeRoom(caller))
    result = await wait_for_speaker(ctx, timeout_s=5.0)  # type: ignore[arg-type]
    assert result == SpeakerReady(caller)  # type: ignore[arg-type]
```

Apply the same `result == SpeakerReady(...)` unwrap to
`test_returns_the_browser_caller_when_it_joins` and
`test_returns_sip_callee_only_after_answer_not_ring`.

Change the three "returns None" tests to expect `CallFailed(NO_ANSWER)` and rename them:

```python
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
```

Add classification + disconnect-driven tests:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/worker/test_wait_for_speaker.py -q`
Expected: FAIL — `ImportError: cannot import name 'CallFailed'` (and others).

- [ ] **Step 3: Implement the result types + classifier + new wait_for_speaker**

In `apps/agent_worker/src/agent_worker/main.py`:

Add imports near the top (after the existing `from livekit import rtc`):

```python
from dataclasses import dataclass

from vera_core.events import CallFailureReason
```

Replace the `_is_ready_speaker` / `wait_for_speaker` region (lines ~56-116) — keep
`_is_ready_speaker` as-is and add the result types + classifier above it, then swap
`wait_for_speaker`:

```python
@dataclass(frozen=True)
class SpeakerReady:
    """A ready, non-monitor participant is present — the agent may start/greet."""

    participant: rtc.RemoteParticipant


@dataclass(frozen=True)
class CallFailed:
    """The outbound call did not connect (busy/declined/no-answer/trunk error)."""

    reason: CallFailureReason


type WaitResult = SpeakerReady | CallFailed

# SIP disconnect reason (int enum) → user-facing failure class. Anything not listed
# (incl. None and SIP_TRUNK_FAILURE) is an opaque failure.
_SIP_FAILURE_REASONS: dict[int, CallFailureReason] = {
    rtc.DisconnectReason.USER_REJECTED: CallFailureReason.BUSY_OR_DECLINED,
    rtc.DisconnectReason.USER_UNAVAILABLE: CallFailureReason.NO_ANSWER,
}


def classify_sip_disconnect(reason: int | None) -> CallFailureReason:
    if reason is None:
        return CallFailureReason.FAILED
    return _SIP_FAILURE_REASONS.get(reason, CallFailureReason.FAILED)
```

Replace `wait_for_speaker` with:

```python
async def wait_for_speaker(ctx: JobContext, timeout_s: float = _SPEAKER_TIMEOUT_S) -> WaitResult:
    """Block until the call is ready to run or has failed.

    Returns SpeakerReady once the browser caller joins or the SIP callee answers
    (sip.callStatus == "active"). Returns CallFailed if the SIP callee drops before
    answering (busy/declined/trunk error) or nobody becomes ready within timeout_s
    (treated as no-answer). Subscribe to events BEFORE scanning existing participants
    so a join/attr/disconnect in the gap is never missed.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[WaitResult] = loop.create_future()

    def _resolve_ready(participant: rtc.Participant) -> None:
        if result.done() or not _is_ready_speaker(participant):
            return
        remote = ctx.room.remote_participants.get(participant.identity)
        if remote is not None:
            result.set_result(SpeakerReady(remote))

    def _on_connected(participant: rtc.RemoteParticipant) -> None:
        _resolve_ready(participant)

    def _on_attributes_changed(_changed: dict[str, str], participant: rtc.Participant) -> None:
        _resolve_ready(participant)

    def _on_disconnected(participant: rtc.RemoteParticipant) -> None:
        # A SIP callee dropping before it answered means the outbound call failed.
        if result.done() or participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return
        result.set_result(CallFailed(classify_sip_disconnect(participant.disconnect_reason)))

    ctx.room.on("participant_connected", _on_connected)
    ctx.room.on("participant_attributes_changed", _on_attributes_changed)
    ctx.room.on("participant_disconnected", _on_disconnected)
    try:
        for p in ctx.room.remote_participants.values():
            _resolve_ready(p)
        if result.done():
            return result.result()
        async with asyncio.timeout(timeout_s):
            return await result
    except TimeoutError:
        return CallFailed(CallFailureReason.NO_ANSWER)
    finally:
        ctx.room.off("participant_connected", _on_connected)
        ctx.room.off("participant_attributes_changed", _on_attributes_changed)
        ctx.room.off("participant_disconnected", _on_disconnected)
```

> Note: `entrypoint` still calls `wait_for_speaker` and checks `speaker is None`; it
> will not type-check until Task 4. That's expected — Task 4 rewires it. If you run the
> whole worker test module now, only `test_wait_for_speaker.py` is asserted here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/worker/test_wait_for_speaker.py -q`
Expected: PASS (all updated + new cases).

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/main.py tests/unit/worker/test_wait_for_speaker.py
git commit -m "feat(worker): classify SIP disconnect; wait_for_speaker returns SpeakerReady|CallFailed"
```

---

### Task 4: Worker — emit `call.failed` and drop the transcript notice

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/main.py` (delete `publish_unanswered_notice` ~lines 145-154; rewire `entrypoint` ~lines 197-217; add `_emit_call_failed`)
- Test: `tests/unit/worker/test_emit_call_failed.py`

**Interfaces:**
- Consumes: `CallFailed`, `CallFailureReason` (Task 3), `WorkerEventBus`, `CallFailedEvent` (Task 1)
- Produces: `async def _emit_call_failed(bus: WorkerEventBus, room_name: str, reason: CallFailureReason, *, now_ms: int) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/worker/test_emit_call_failed.py`:

```python
"""The worker publishes a typed call.failed event when an outbound call fails."""

import pytest

from agent_worker.main import _emit_call_failed
from vera_core.events import CallFailedEvent, CallFailureReason, WorkerEvent, parse_worker_event


class _RecordingBus:
    def __init__(self) -> None:
        self.emitted: list[WorkerEvent] = []

    async def emit(self, event: WorkerEvent) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_emit_call_failed_publishes_typed_event() -> None:
    bus = _RecordingBus()
    await _emit_call_failed(
        bus,  # type: ignore[arg-type]
        "call--t--c",
        CallFailureReason.BUSY_OR_DECLINED,
        now_ms=1720000000000,
    )
    assert bus.emitted == [
        CallFailedEvent(
            room_name="call--t--c",
            reason=CallFailureReason.BUSY_OR_DECLINED,
            ts=1720000000000,
        )
    ]
    # And it is wire-serializable.
    assert parse_worker_event(bus.emitted[0].model_dump_json()) == bus.emitted[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/worker/test_emit_call_failed.py -q`
Expected: FAIL — `ImportError: cannot import name '_emit_call_failed'`.

- [ ] **Step 3: Implement emit + rewire entrypoint**

In `apps/agent_worker/src/agent_worker/main.py`:

Add to imports (with the Task 3 import of `CallFailureReason`):

```python
from vera_core.events import CallFailedEvent, CallFailureReason, WorkerEventBus
```

Delete `publish_unanswered_notice` (the whole function, ~lines 145-154).

Add near the other module-level helpers:

```python
async def _emit_call_failed(
    bus: WorkerEventBus, room_name: str, reason: CallFailureReason, *, now_ms: int
) -> None:
    """Publish the call.failed event the control plane consumes to tear the room down."""
    await bus.emit(CallFailedEvent(room_name=room_name, reason=reason, ts=now_ms))
```

Replace the `if meta.get("wait_for_speaker"):` block in `entrypoint` (~lines 199-217) with:

```python
    speaker: rtc.RemoteParticipant | None = None
    meta = json.loads(ctx.job.metadata or "{}")
    if meta.get("wait_for_speaker"):
        outcome = await wait_for_speaker(ctx)
        if isinstance(outcome, CallFailed):
            logger.warning("outbound call failed for room %s: %s", room_name, outcome.reason.value)
            failure_redis = create_redis(settings.redis_url)
            try:
                await _emit_call_failed(
                    WorkerEventBus(failure_redis, maxlen=settings.worker_events_stream_maxlen),
                    room_name,
                    outcome.reason,
                    now_ms=int(time.time() * 1000),
                )
            finally:
                await failure_redis.aclose()
            return
        speaker = outcome.participant
```

> This removes the transcript-based `publish_unanswered_notice` path entirely: failures
> now flow only through the event bus. The `speaker` variable feeds
> `build_room_input_options(speaker.identity ...)` unchanged further down.

- [ ] **Step 4: Run test + the whole worker suite**

Run: `uv run pytest tests/unit/worker/ -q`
Expected: PASS (emit test + wait_for_speaker suite). Confirms `entrypoint` still imports.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/main.py tests/unit/worker/test_emit_call_failed.py
git commit -m "feat(worker): emit call.failed event on outbound failure; drop transcript notice"
```

---

### Task 5: Control plane — `WorkerEventConsumer`

**Files:**
- Create: `apps/control_plane/src/control_plane/worker_events.py`
- Test: `tests/unit/control_plane/test_worker_events.py`

**Interfaces:**
- Consumes: `WorkerEventBus`, `parse_worker_event`, `CallFailedEvent`, `WORKER_EVENTS_STREAM`, `WORKER_EVENTS_GROUP` (Task 1); `LiveKitGateway.set_room_metadata` + `delete_room` (Task 2); `parse_room_name`
- Produces:
  - `class WorkerEventConsumer`: `__init__(redis, livekit, *, block_ms=5000, reclaim_idle_ms=60000, teardown_grace_ms=1500, consumer_name=None)`
  - `async run() -> None`; internal `_process`, `_handle_call_failed`, `_ack`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/control_plane/test_worker_events.py`:

```python
"""Unit tests for the control-plane worker-event consumer (no live Redis)."""

import pytest

from control_plane.worker_events import WorkerEventConsumer
from vera_core.events import CallFailedEvent, CallFailureReason


class _FakeLiveKit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_metadata = False

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        if self.fail_metadata:
            raise RuntimeError("boom")
        self.calls.append(("meta", (room_name, metadata)))

    async def delete_room(self, room_name: str) -> None:
        self.calls.append(("delete", room_name))


class _FakeRedis:
    def __init__(self) -> None:
        self.acked: list[str] = []

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append(entry_id)
        return 1


def _consumer(redis: _FakeRedis, livekit: _FakeLiveKit) -> WorkerEventConsumer:
    return WorkerEventConsumer(redis, livekit, teardown_grace_ms=0)  # type: ignore[arg-type]


def _event_fields(room: str = "call--t--c") -> dict[str, str]:
    ev = CallFailedEvent(room_name=room, reason=CallFailureReason.NO_ANSWER, ts=1)
    return {"event": ev.model_dump_json()}


@pytest.mark.asyncio
async def test_handle_call_failed_sets_metadata_then_deletes() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("1-0", _event_fields())
    assert livekit.calls == [
        ("meta", ("call--t--c", {"status": "call_failed", "reason": "no_answer"})),
        ("delete", "call--t--c"),
    ]
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_ignores_non_vera_room() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("2-0", _event_fields(room="lobby"))
    assert livekit.calls == []  # not torn down
    assert redis.acked == ["2-0"]  # but acked (nothing to retry)


@pytest.mark.asyncio
async def test_missing_event_field_is_acked_and_skipped() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("3-0", {"nope": "x"})
    assert livekit.calls == []
    assert redis.acked == ["3-0"]


@pytest.mark.asyncio
async def test_unparseable_event_is_dropped() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("4-0", {"event": "{not json"})
    assert livekit.calls == []
    assert redis.acked == ["4-0"]


@pytest.mark.asyncio
async def test_handler_failure_leaves_entry_unacked() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    livekit.fail_metadata = True
    await _consumer(redis, livekit)._process("5-0", _event_fields())
    assert redis.acked == []  # left pending for XAUTOCLAIM to retry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/control_plane/test_worker_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'control_plane.worker_events'`.

- [ ] **Step 3: Implement the consumer**

Create `apps/control_plane/src/control_plane/worker_events.py`:

```python
"""Consumes worker→control-plane events (Redis Streams consumer group) and
orchestrates the reaction. One consumer runs per control-plane process; the group
delivers each event to exactly one process, and entries a crashed process left
pending are reclaimed via XAUTOCLAIM (at-least-once). Handlers are idempotent, so
redelivery / a rare double-delivery is harmless.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from control_plane.livekit_gateway import LiveKitGateway
from vera_core.events import (
    WORKER_EVENTS_GROUP,
    WORKER_EVENTS_STREAM,
    CallFailedEvent,
    WorkerEvent,
    WorkerEventBus,
    parse_worker_event,
)
from vera_core.observability.correlation import parse_room_name

logger = logging.getLogger("control_plane.worker_events")

type EventHandler = Callable[[WorkerEvent], Awaitable[None]]


class WorkerEventConsumer:
    def __init__(
        self,
        redis: Redis,
        livekit: LiveKitGateway,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        teardown_grace_ms: int = 1_500,
        consumer_name: str | None = None,
    ) -> None:
        self._redis = redis
        self._livekit = livekit
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._teardown_grace_ms = teardown_grace_ms
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._bus = WorkerEventBus(redis)
        self._handlers: dict[str, EventHandler] = {"call.failed": self._handle_call_failed}

    async def run(self) -> None:
        """Ensure the group exists, then loop: reclaim stragglers, read new, dispatch."""
        await self._bus.ensure_group()
        while True:
            try:
                await self._reclaim_stale()
                await self._read_once()
            except asyncio.CancelledError:
                raise
            except RedisError:
                logger.exception("worker-event consumer Redis error; backing off")
                await asyncio.sleep(1.0)

    async def _read_once(self) -> None:
        resp = await self._redis.xreadgroup(
            WORKER_EVENTS_GROUP,
            self._consumer,
            {WORKER_EVENTS_STREAM: ">"},
            count=16,
            block=self._block_ms,
        )
        if not resp:
            return
        _stream, entries = resp[0]
        await asyncio.gather(*(self._process(entry_id, fields) for entry_id, fields in entries))

    async def _reclaim_stale(self) -> None:
        _cursor, entries, _deleted = await self._redis.xautoclaim(
            WORKER_EVENTS_STREAM,
            WORKER_EVENTS_GROUP,
            self._consumer,
            min_idle_time=self._reclaim_idle_ms,
            start_id="0-0",
            count=16,
        )
        await asyncio.gather(*(self._process(entry_id, fields) for entry_id, fields in entries))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        raw = fields.get("event")
        if raw is None:
            logger.warning("worker event %s has no payload; dropping", entry_id)
            await self._ack(entry_id)
            return
        try:
            event = parse_worker_event(raw)
        except Exception:
            logger.exception("dropping unparseable worker event %s", entry_id)
            await self._ack(entry_id)  # poison entry — drop so it can't wedge the group
            return
        handler = self._handlers.get(event.type)
        if handler is None:
            logger.warning("no handler for worker event type %s; dropping", event.type)
            await self._ack(entry_id)
            return
        try:
            await handler(event)
        except Exception:
            logger.exception("handler failed for event %s; leaving unacked for reclaim", entry_id)
            return  # do NOT ack → XAUTOCLAIM retries later (at-least-once)
        await self._ack(entry_id)

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP, entry_id)

    async def _handle_call_failed(self, event: WorkerEvent) -> None:
        assert isinstance(event, CallFailedEvent)
        if parse_room_name(event.room_name) is None:
            logger.warning("call.failed for non-vera room %s; ignoring", event.room_name)
            return
        await self._livekit.set_room_metadata(
            event.room_name, {"status": "call_failed", "reason": event.reason.value}
        )
        # Let the RoomMetadataChanged frame reach the browser before we tear the room down.
        if self._teardown_grace_ms:
            await asyncio.sleep(self._teardown_grace_ms / 1000)
        await self._livekit.delete_room(event.room_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/control_plane/test_worker_events.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/worker_events.py \
        tests/unit/control_plane/test_worker_events.py
git commit -m "feat(control-plane): worker-event consumer (Streams group) tears down room on call.failed"
```

---

### Task 6: Control plane — start the consumer in the app lifespan

**Files:**
- Modify: `apps/control_plane/src/control_plane/main.py` (imports; lifespan body ~lines 86-118)
- Test: `tests/integration/control_plane/test_app_boot.py` (new, light)

**Interfaces:**
- Consumes: `WorkerEventConsumer` (Task 5), `create_redis`, `settings.livekit_url`, `settings.worker_events_*`, `settings.call_failed_teardown_grace_ms`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/control_plane/test_app_boot.py`:

```python
"""The app must boot cleanly whether or not the worker-event consumer starts.

When livekit_url is unset (tests/local without SIP), the consumer is not started,
so no Redis stream connection is attempted during app startup.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.main import create_app
from vera_core.config import Settings


@pytest.mark.asyncio
async def test_app_boots_without_consumer_when_livekit_unset() -> None:
    app = create_app(settings=Settings(_env_file=None, livekit_url=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # Lifespan runs on the first request; a bare 404 proves startup/shutdown are clean.
        resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails or errors**

Run: `uv run pytest tests/integration/control_plane/test_app_boot.py -q`
Expected: At this point the app still boots (consumer code not wired), so this may PASS
trivially. That is fine — it is the regression guard for Step 3. Proceed.

- [ ] **Step 3: Wire the consumer into the lifespan**

In `apps/control_plane/src/control_plane/main.py`:

Add imports:

```python
import asyncio
from contextlib import asynccontextmanager, suppress
```

(replace the existing `from contextlib import asynccontextmanager` line), and:

```python
from control_plane.worker_events import WorkerEventConsumer
```

Inside `lifespan`, after `app.state.invitation_store = ...` (line 111) and before
`configure_observability(settings)` (line 112), add:

```python
        # Worker→control-plane event consumer. Needs a real LiveKit gateway (to tear
        # rooms down) and a dedicated Redis client (a blocking XREADGROUP pins a
        # connection — same reason the transcript stream gets its own client). Not
        # started when SIP/LiveKit is unconfigured (tests / local without a trunk).
        worker_events_redis: Redis | None = None
        worker_event_task: asyncio.Task[None] | None = None
        if settings.livekit_url is not None and app.state.livekit is not None:
            worker_events_redis = create_redis(settings.redis_url)
            consumer = WorkerEventConsumer(
                worker_events_redis,
                app.state.livekit,
                block_ms=settings.worker_events_block_ms,
                reclaim_idle_ms=settings.worker_events_reclaim_idle_ms,
                teardown_grace_ms=settings.call_failed_teardown_grace_ms,
            )
            worker_event_task = asyncio.create_task(consumer.run())
```

Then extend the shutdown section (after `yield`, before `if redis is not None:`) with:

```python
        if worker_event_task is not None:
            worker_event_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_event_task
        if worker_events_redis is not None:
            await worker_events_redis.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_app_boot.py tests/integration/control_plane/test_voice_lab.py -q`
Expected: PASS (boot guard + existing Voice Lab suite unaffected — consumer stays off with `livekit_url=None`).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/main.py tests/integration/control_plane/test_app_boot.py
git commit -m "feat(control-plane): start worker-event consumer in app lifespan when LiveKit is configured"
```

---

### Task 7: Backend gate — lint, types, full suite

**Files:** none (verification task)

- [ ] **Step 1: Run the full backend gate**

Run (from `vera-backend/`): `just check`
Expected: ruff clean, mypy --strict clean, all pytest green. Fix any lint/type findings
inline (e.g. an unused import, a missing annotation) and re-run until green.

> mypy note: `wait_for_speaker`'s callers now branch on `isinstance(outcome, CallFailed)`;
> ensure no remaining `speaker is None` references survive from the old contract.

- [ ] **Step 2: Commit any fixups**

```bash
git add -A
git commit -m "chore(backend): satisfy ruff + mypy for worker event bus"
```

---

### Task 8: Frontend — pure call-failure parser

**Files:**
- Create: `vera-frontend/src/lib/voice-lab/callFailure.ts`
- Test: `vera-frontend/src/lib/voice-lab/callFailure.test.ts`

**Interfaces:**
- Produces:
  - `type CallFailureReason = "no_answer" | "busy_or_declined" | "failed"`
  - `function callFailureMessage(reason: CallFailureReason): string`
  - `function parseCallFailure(metadata: string | undefined): string | null`

- [ ] **Step 1: Write the failing test**

Create `vera-frontend/src/lib/voice-lab/callFailure.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { parseCallFailure } from "./callFailure"

describe("parseCallFailure", () => {
  it("returns null for absent metadata", () => {
    expect(parseCallFailure(undefined)).toBeNull()
    expect(parseCallFailure("")).toBeNull()
  })

  it("returns null for unparseable metadata", () => {
    expect(parseCallFailure("{not json")).toBeNull()
  })

  it("returns null when status is not call_failed", () => {
    expect(parseCallFailure(JSON.stringify({ status: "active" }))).toBeNull()
  })

  it("maps each known reason to its message", () => {
    expect(parseCallFailure(JSON.stringify({ status: "call_failed", reason: "no_answer" }))).toMatch(
      /wasn't answered/i,
    )
    expect(
      parseCallFailure(JSON.stringify({ status: "call_failed", reason: "busy_or_declined" })),
    ).toMatch(/declined or the line was busy/i)
    expect(parseCallFailure(JSON.stringify({ status: "call_failed", reason: "failed" }))).toMatch(
      /couldn't be completed/i,
    )
  })

  it("falls back to a generic message for an unknown reason", () => {
    expect(parseCallFailure(JSON.stringify({ status: "call_failed", reason: "??" }))).toMatch(
      /could not be completed/i,
    )
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `vera-frontend/`): `npx vitest run src/lib/voice-lab/callFailure.test.ts`
Expected: FAIL — cannot find module `./callFailure`.

- [ ] **Step 3: Implement the parser**

Create `vera-frontend/src/lib/voice-lab/callFailure.ts`:

```ts
// Interpreting the LiveKit room-metadata "call_failed" signal the control plane sets
// when an outbound call fails. The backend event carries only a reason code; the UI
// owns the human-facing copy (kept here so it is unit-testable without React).

export type CallFailureReason = "no_answer" | "busy_or_declined" | "failed"

const MESSAGES: Record<CallFailureReason, string> = {
  no_answer: "The call wasn't answered — it rang but nobody picked up.",
  busy_or_declined: "The call was declined or the line was busy.",
  failed: "The call couldn't be completed. Check the number and try again.",
}

const GENERIC = "The call could not be completed."

export function callFailureMessage(reason: CallFailureReason): string {
  return MESSAGES[reason]
}

/** Parse LiveKit room metadata (a JSON string) for a call-failure signal. Returns the
 *  user-facing message, or null if metadata is absent, unparseable, or not a
 *  call_failed status. An unknown reason yields a generic (non-null) message. */
export function parseCallFailure(metadata: string | undefined): string | null {
  if (!metadata) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(metadata)
  } catch {
    return null
  }
  if (typeof parsed !== "object" || parsed === null) return null
  const record = parsed as { status?: unknown; reason?: unknown }
  if (record.status !== "call_failed") return null
  if (record.reason === "no_answer" || record.reason === "busy_or_declined" || record.reason === "failed") {
    return callFailureMessage(record.reason)
  }
  return GENERIC
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `vera-frontend/`): `npx vitest run src/lib/voice-lab/callFailure.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/voice-lab/callFailure.ts vera-frontend/src/lib/voice-lab/callFailure.test.ts
git commit -m "feat(voice-lab): pure parser mapping room-metadata call-failure to a message"
```

---

### Task 9: Frontend — surface the failure in Voice Lab

**Files:**
- Modify: `vera-frontend/src/pages/VoiceLab.tsx`

**Interfaces:**
- Consumes: `parseCallFailure` (Task 8), `useRoomInfo` (`@livekit/components-react`)

- [ ] **Step 1: Add the metadata watcher component**

In `vera-frontend/src/pages/VoiceLab.tsx`, extend the components-react import to include
`useRoomInfo`:

```tsx
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useParticipants,
  useRoomInfo,
} from "@livekit/components-react"
```

Add the import for the parser:

```tsx
import { parseCallFailure } from "@/lib/voice-lab/callFailure"
```

Add this component near `SessionPanel` (it must live inside `<LiveKitRoom>` to read room info):

```tsx
/** Watches LiveKit room metadata for the control plane's "call_failed" signal and
 *  reports it once. Must render inside <LiveKitRoom>. */
function CallFailureWatcher({ onFailure }: { onFailure: (message: string) => void }) {
  const { metadata } = useRoomInfo()
  const fired = useRef(false)
  useEffect(() => {
    if (fired.current) return
    const message = parseCallFailure(metadata)
    if (message) {
      fired.current = true
      onFailure(message)
    }
  }, [metadata, onFailure])
  return null
}
```

- [ ] **Step 2: Split auto-disconnect from user-end, add failSession**

In the `VoiceLab` component, replace the single `endSession` usage in `SessionPanel` so the
auto-cleanup does not clear a failure message. Add these callbacks alongside `endSession`:

```tsx
  // Auto-cleanup when the room disconnects (agent deleted it, network drop). Resets to the
  // form but preserves any error already set (e.g. a call-failure message).
  const resetSession = useCallback(() => {
    setSession(null)
  }, [])

  // A failed outbound call: show why, then drop back to the form. The control plane has
  // already (or is about to) delete the room server-side, so no DELETE call is needed here.
  const failSession = useCallback((message: string) => {
    setError(message)
    setSession(null)
  }, [])
```

Change `SessionPanel` to take a distinct `onDisconnected` prop and call it from the effect
(instead of `onEnd`). Update its signature and effect:

```tsx
function SessionPanel({
  mode,
  onEnd,
  onDisconnected,
  actions,
}: {
  mode: VoiceSessionMode
  onEnd: () => void
  onDisconnected: () => void
  actions?: ReactNode
}) {
  const state = useConnectionState()
  const participants = useParticipants()
  const wasConnected = useRef(false)

  useEffect(() => {
    if (state === ConnectionState.Connected) {
      wasConnected.current = true
    }
    if (wasConnected.current && state === ConnectionState.Disconnected) {
      onDisconnected()
    }
  }, [state, onDisconnected])
```

(The `End session` button keeps `onClick={onEnd}` — unchanged.)

- [ ] **Step 3: Wire the watcher + props in the render tree**

In the `session ? (...)` branch, add `<CallFailureWatcher>` inside `<LiveKitRoom>` and pass
`onDisconnected` to `SessionPanel`:

```tsx
        <LiveKitRoom
          serverUrl={session.url}
          token={session.token}
          connect
          audio={session.mode === "browser"}
          video={false}
          onError={(e) => setError(e.message)}
        >
          <CallFailureWatcher onFailure={failSession} />
          <div className="space-y-6">
            <SessionPanel
              mode={session.mode}
              onEnd={endSession}
              onDisconnected={resetSession}
              actions={
                session.mode === "outbound" ? <VoiceLabDialpad onError={setError} /> : undefined
              }
            />
            <TranscriptPanel key={session.room_name} roomName={session.room_name} />
          </div>
          <RoomAudioRenderer />
        </LiveKitRoom>
```

- [ ] **Step 4: Typecheck, lint, unit tests, build**

Run (from `vera-frontend/`):
```
npx tsc -b
npx eslint src/pages/VoiceLab.tsx src/lib/voice-lab/callFailure.ts
npx vitest run
npm run build
```
Expected: no type errors, no lint errors, all vitest green, build succeeds.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/pages/VoiceLab.tsx
git commit -m "feat(voice-lab): show a call-failure banner from room metadata and reset the session"
```

---

### Task 10: End-to-end verification

**Files:** none (verification task)

- [ ] **Step 1: Backend gate**

Run (from `vera-backend/`): `just check`
Expected: all green.

- [ ] **Step 2: Frontend gate**

Run (from `vera-frontend/`): `npm run build && npx eslint . && npx vitest run`
Expected: all green.

- [ ] **Step 3: Drive the real flow (use the `verify` skill)**

Bring up local infra (`just up` in `vera-backend/`, plus `just migrate`), start the control
plane (`just api`) and the worker (`just worker`), and open Voice Lab. With a configured
outbound trunk, dial a number and force each failure:
- Decline the call on the phone → banner: "declined or the line was busy"; session resets.
- Let it ring out / no answer → banner: "wasn't answered"; session resets within the ring timeout.
- Confirm the room is gone server-side after each (no orphaned room; the browser leaves).
If a trunk is not available locally, note it and rely on the unit/integration coverage plus
a manual check against a staging trunk.

- [ ] **Step 4: Simplify pass (project rule)**

Run the `/simplify` skill on the changed backend + frontend files (quality-only), then
re-run both gates (`just check`; frontend build+lint+test). Commit any refinements.

```bash
git add -A
git commit -m "refactor(voice-lab): simplify-pass cleanups for call-failure handling"
```

---

## Self-Review

**Spec coverage:**
- §5.1 shared event contract → Task 1. §5.2 worker detect/emit → Tasks 3, 4. §5.3 CP consumer + `set_room_metadata` + lifespan → Tasks 2, 5, 6. §5.4 frontend → Tasks 8, 9. §6 failure matrix → Tasks 3 (classification) + 5 (handler). §7 edge cases (N replicas / reclaim / trimming / grace / browser-absent / LiveKit-unset) → Tasks 1 (maxlen), 5 (`_reclaim_stale`, grace, idempotent handler), 6 (gating). §8 tests → every task's test step. §9 out-of-scope respected (no SIP numeric code; `/calls` untouched; transcript stream untouched).
- Ticket's synchronous dial-rejection path (`OutboundDialError` → 502 → form banner) is intentionally unchanged and still covered by existing Voice Lab tests.

**Placeholder scan:** No TBD/TODO; every code step has complete code; no "handle errors appropriately" hand-waving.

**Type consistency:** `WorkerEventConsumer.__init__` params (`block_ms`, `reclaim_idle_ms`, `teardown_grace_ms`) match the Task 6 lifespan call and the `settings.worker_events_*` / `call_failed_teardown_grace_ms` names from Task 1. `wait_for_speaker → WaitResult` (`SpeakerReady | CallFailed`) is produced in Task 3 and consumed in Task 4 (`isinstance(outcome, CallFailed)`, `outcome.participant`). `set_room_metadata(room_name, metadata)` signature matches across Task 2 (gateway + fake) and Task 5 (`_handle_call_failed`). `parseCallFailure(metadata)` signature matches Task 8 export and Task 9 usage. Metadata shape `{"status":"call_failed","reason":<value>}` is written in Task 5 and read in Task 8.
