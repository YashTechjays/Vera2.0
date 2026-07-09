# Worker→Control-Plane Event Bus + Outbound Call-Failure Handling

**Date:** 2026-07-07
**Status:** Approved design, ready for implementation plan
**Scope:** `vera-backend` (agent worker, control plane, `vera_core`) + `vera-frontend` (Voice Lab)

## 1. Purpose

Fix the Voice Lab outbound-call flow so that when a call fails — the callee's line
is busy, the callee declines, nobody answers, or the SIP trunk / carrier errors — the
LiveKit session is **automatically closed** and the browser shows a **proper, specific
error message** explaining why.

To do this correctly (and reusably) we introduce a **first-class worker→control-plane
event bus** over **Redis Streams + a consumer group**, rather than overloading the
existing transcript stream. Call-failure is the first event carried; the bus is built
so future worker→control-plane signals (e.g. the real `/calls` flow's call-status
transitions) reuse the same transport.

## 2. Background — the three gaps (root cause)

The current outbound flow (`api/v1/voice_lab.py` → `LiveKitGateway.create_sip_participant`
→ agent worker `wait_for_speaker`):

1. **`create_sip_participant(wait_until_answered=False)`** returns as soon as LiveKit
   *accepts the dial request*. Busy / declined / no-answer / post-accept carrier errors
   all happen *after* it returns and therefore never raise `OutboundDialError`. Only a
   *synchronous* dial-time rejection (bad/deleted trunk, transport error) is caught.
2. **The worker's `wait_for_speaker` 60s timeout is the only net.** On timeout it
   publishes a *generic* "not answered or unavailable" **transcript** line, then
   `return`s **without deleting the room** and without starting the `AgentSession` —
   so the framework's `delete_room_on_close` teardown never runs and the room lingers.
   It also always waits the full 60s, even for an instant busy/decline.
3. **The frontend has no structured failure channel.** The transcript SSE only carries
   `{role, text, ts}` turns, and the UI only resets on a LiveKit `Disconnected` event,
   which never fires because the room isn't deleted.

## 3. Why the control plane dials (a constraint that shapes this design)

Outbound dialing stays in the **control plane** — this is intentional and documented
(`2026-06-29-livekit-trunk-from-integrations-design.md`): "The agent worker has no DB
access and does not dial." Three constraints:

- The tenant's outbound `trunk_id` is envelope-encrypted in the `integration` table;
  resolving + KMS-decrypting it needs DB + KMS access, which only the control plane has.
- Credentials decrypt at dial time on the control-plane side.
- **PHI boundary:** making the worker dial would require passing the callee **phone
  number** (PHI) through **dispatch metadata** (LiveKit room metadata) — a bright-line
  violation ("raw PHI never goes in a room name / metadata").

LiveKit supports both worker-initiated (`JobContext.add_sip_participant`) and
server-initiated (`sip.create_sip_participant`) dialing; Vera deliberately uses
server-initiated. **Consequence:** the worker cannot rely on
`create_sip_participant(wait_until_answered=True)` raising to detect failure — but it
*can* observe the SIP callee's `participant_disconnected` + `disconnect_reason`. That
observation is the source of the failure event.

## 4. Architecture overview

```
worker (agent process, DB-less)
  wait_for_speaker(ctx) ──► SpeakerReady(participant) | CallFailed(reason)
        │ on CallFailed:
        ▼ WorkerEventBus.emit(CallFailedEvent{room_name, reason, ts})
        Redis Stream  XADD  vera:worker-events   (MAXLEN ~ trimmed)
        │
        ▼ XREADGROUP (consumer group "control-plane", one delivery per event)
control plane (N replicas, each a group consumer)
  WorkerEventConsumer.run()  ──► dispatch by event.type ──► handler("call.failed"):
        1. livekit.set_room_metadata(room, {status:"call_failed", reason})
        2. short grace (propagation)
        3. livekit.delete_room(room)          # server-authoritative auto-close
        4. XACK
        │
        ▼ LiveKit WS (room metadata changed, then room deleted)
browser (Voice Lab, listen-only monitor)
  useRoomInfo() sees metadata.status == "call_failed"
        ──► map reason → message, show ErrorAlert, reset to form
        (subsequent "Disconnected" confirms teardown; does not clobber the message)
```

Key properties:

- **Exactly-once across N control-plane instances:** a consumer *group* load-balances —
  each event is delivered to exactly one replica, not fanned out to all (the pub/sub
  flaw). The stream persists, so an event published while consumers are momentarily
  down is not lost (at-least-once via the pending-entries list + `XAUTOCLAIM`).
- **Self-describing messages:** the call identity is in the payload (`room_name`, which
  embeds tenant + call UUIDs), not the channel name — one generic stream, no ambiguity.
- **No PHI on the bus:** events carry only `room_name` (UUIDs), a reason enum, and a
  timestamp — never phone number or transcript text.
- **Idempotent handlers:** safe under at-least-once redelivery and safe if two replicas
  ever double-process (`delete_room` swallows `not_found`; metadata is last-write-wins).

## 5. Components

### 5.1 Shared event contract — `vera_core/events/` (new)

The wire contract, importable by both processes. PHI-free by construction.

```python
# vera_core/events/worker.py
WORKER_EVENTS_STREAM = "vera:worker-events"
WORKER_EVENTS_GROUP = "control-plane"

class CallFailureReason(StrEnum):
    NO_ANSWER = "no_answer"                 # USER_UNAVAILABLE, or 60s wait timeout
    BUSY_OR_DECLINED = "busy_or_declined"   # USER_REJECTED
    FAILED = "failed"                       # SIP_TRUNK_FAILURE / other pre-answer drop

class CallFailedEvent(BaseModel):
    type: Literal["call.failed"] = "call.failed"
    room_name: str
    reason: CallFailureReason
    ts: int

# Discriminated union — grows as new event types are added.
WorkerEvent = Annotated[CallFailedEvent, Field(discriminator="type")]

def parse_worker_event(raw: str) -> WorkerEvent: ...   # TypeAdapter, raises on bad data
```

Transport wrapper (thin, over `redis.asyncio`):

```python
class WorkerEventBus:
    def __init__(self, redis: Redis, *, maxlen: int = 10_000) -> None: ...
    async def emit(self, event: WorkerEvent) -> None:
        # XADD with approximate MAXLEN trimming to bound stream memory.
        await self._redis.xadd(
            WORKER_EVENTS_STREAM,
            {"event": event.model_dump_json()},
            maxlen=self._maxlen, approximate=True,
        )
    async def ensure_group(self) -> None:
        # XGROUP CREATE ... MKSTREAM; ignore BUSYGROUP if it already exists.
        ...
```

`maxlen` and any tunables live on `Settings` with sensible defaults.

### 5.2 Worker — detect + emit (`agent_worker/main.py`)

- `wait_for_speaker(ctx)` return type changes from `RemoteParticipant | None` to a small
  discriminated result:

  ```python
  @dataclass(frozen=True)
  class SpeakerReady:
      participant: rtc.RemoteParticipant
  @dataclass(frozen=True)
  class CallFailed:
      reason: CallFailureReason
  ```

- Add a `participant_disconnected` handler alongside the existing
  `participant_connected` / `participant_attributes_changed` ones. If the **SIP callee**
  disconnects **before** a ready speaker resolved, resolve `CallFailed(classify(reason))`.
  Classification (verified against the installed `livekit.rtc` `DisconnectReason`):

  | `DisconnectReason` | `CallFailureReason` |
  |---|---|
  | `USER_REJECTED` | `BUSY_OR_DECLINED` |
  | `USER_UNAVAILABLE` | `NO_ANSWER` |
  | `SIP_TRUNK_FAILURE` / anything else pre-answer | `FAILED` |

  The 60s timeout resolves `CallFailed(NO_ANSWER)` (replacing today's `None`). A
  busy/decline now resolves the instant LiveKit sends the disconnect — no 60s wait.

- In `entrypoint`, when the outcome is `CallFailed`: emit `CallFailedEvent(room_name,
  reason, ts)` via `WorkerEventBus` and return. The worker **no longer touches the
  transcript or the room for failures**. `publish_unanswered_notice` is deleted.

- The worker gets a short-lived Redis client to emit (mirrors how the current timeout
  branch already builds one), closed in a `finally`.

Post-answer hangup is unchanged: it's already handled by the framework's
`close_on_disconnect` + `delete_room_on_close` once the session has started.

### 5.3 Control-plane consumer — `control_plane/worker_events.py` (new)

```python
class WorkerEventConsumer:
    def __init__(self, redis: Redis, livekit: LiveKitGateway,
                 handlers: Mapping[str, EventHandler] | None = None) -> None: ...
    async def run(self) -> None:
        # ensure group exists; loop XREADGROUP BLOCK with a unique consumer name;
        # parse defensively; dispatch each event to its handler AS A TASK; XACK on
        # success; periodic XAUTOCLAIM to reclaim a crashed consumer's pending entries;
        # reconnect-with-backoff on Redis errors; graceful stop on cancel.
```

- **Consumer name** is unique per replica (host/pid) so the group balances correctly.
- **Defensive parsing:** bad JSON / unknown `type` → log + `XACK` (drop, don't poison
  the group) + continue. Never crash the loop.
- **Handler isolation:** each handler runs in its own task; an exception is logged and
  does not stall the consume loop. A message is `XACK`ed only after its handler succeeds
  (so a crash mid-handle leaves it pending for `XAUTOCLAIM` → at-least-once).
- **`call.failed` handler:** `parse_room_name` guard → `set_room_metadata(room,
  {"status": "call_failed", "reason": reason})` → short grace (a named constant, allows
  the metadata-changed frame to reach the browser before teardown) → `delete_room(room)`.

- **Lifespan wiring (`main.py`):** create a **dedicated** Redis client (a blocking
  `XREADGROUP` pins a connection — same reasoning as the transcript pool), build the
  consumer, `asyncio.create_task(consumer.run())`; cancel + await it and close the Redis
  client on shutdown. Started **only when `app.state.livekit` is present**; injectable /
  skippable so tests don't spin up a live consumer.

- **`LiveKitGateway`** gains `set_room_metadata(room_name, metadata: dict[str, object])`
  wrapping `room.update_room_metadata`; `delete_room` already exists.

### 5.4 Frontend — surface the reason (`vera-frontend/src/pages/VoiceLab.tsx`)

- Read room metadata via `useRoomInfo()` (`@livekit/components-react`, already a dep),
  inside `<LiveKitRoom>`. Parse `{status, reason}`; when `status === "call_failed"`,
  map `reason` → user-facing copy (**the frontend owns the message text**; the event
  carries only the reason code):

  | reason | message |
  |---|---|
  | `busy_or_declined` | "The call was declined or the line was busy." |
  | `no_answer` | "The call wasn't answered — it rang but nobody picked up." |
  | `failed` | "The call couldn't be completed. Check the number and try again." |

- On a `call_failed` status: `setError(message)`, tear the session down to the form.
  The existing `ErrorAlert` renders it as a proper destructive banner (not an agent
  chat bubble).
- Adjust the existing `SessionPanel` `Disconnected` auto-cleanup so it resets to the
  form **without clobbering** a just-set failure message (split the auto-disconnect
  reset from the user-initiated "End session" which clears the error).
- The synchronous dial-rejection path (`OutboundDialError` → 502 → form banner via
  `start()`'s catch) is unchanged and complementary.

## 6. Failure-path matrix

| Failure | Detected by | Surfaced to browser |
|---|---|---|
| Bad/deleted trunk, transport error at dial | control plane `OutboundDialError` (synchronous) | 502 → form `ErrorAlert` (existing) |
| Busy / callee declines | worker `USER_REJECTED` → `busy_or_declined` | event → CP → room metadata → banner |
| No answer / rings out | worker `USER_UNAVAILABLE` or 60s timeout → `no_answer` | event → CP → room metadata → banner |
| SIP/carrier error after accept | worker `SIP_TRUNK_FAILURE`/other → `failed` | event → CP → room metadata → banner |
| Callee answers then hangs up | framework `close_on_disconnect` (post-answer) | LiveKit `Disconnected` → reset (existing) |

## 7. Error handling & edge cases

- **N control-plane replicas:** consumer group → each event processed once. Idempotent
  handlers make any rare double-processing harmless.
- **Consumer down at publish time:** stream persists the event; delivered when a
  consumer returns (at-least-once).
- **Consumer crash mid-handle:** message stays in the pending list; `XAUTOCLAIM`
  reclaims it after an idle threshold → reprocessed (idempotent).
- **Stream growth:** `XADD` with approximate `MAXLEN` trimming bounds memory.
- **Message-delivery vs teardown race:** metadata is set *before* `delete_room`, with a
  short grace, so the browser reads the reason before the room is torn down. If the
  browser still loses the race, it falls back to the generic `Disconnected` reset (no
  specific message) — a graceful degradation, not a hang.
- **Browser absent (tab closed):** CP still deletes the room (server-authoritative), so
  the session closes regardless of any client being present.
- **LiveKit unset (local/CI without SIP):** consumer not started; nothing to do.

## 8. Testing

- **`vera_core/events`:** event serialize → `XADD` → `parse_worker_event` round-trip;
  discriminated-union dispatch; unknown `type` rejected cleanly; assert payloads carry
  no PHI fields.
- **Worker (`test_wait_for_speaker.py`, extended):** each `DisconnectReason` →
  correct `CallFailed` reason; 60s timeout → `NO_ANSWER`; answered → `SpeakerReady`.
- **Worker entrypoint failure path:** emits `CallFailedEvent`; does **not** write the
  transcript or delete the room.
- **Consumer:** a `call.failed` message → `set_room_metadata` then `delete_room`, then
  `XACK`; malformed/unknown message → dropped, loop survives; handler exception is
  isolated and the message is left unacked for reclaim.
- **Frontend:** `useRoomInfo` failure metadata → banner + reset; the disconnect handler
  does not clobber the failure message.

## 9. Out of scope

- Precise SIP numeric status code (486/603/408) — not reliably exposed client-side; the
  3-way `disconnect_reason` classification is sufficient.
- Wiring the production `/calls` flow to dial out (it creates the room but does not dial
  yet); it will reuse this event bus when it does.
- Persistence of failure events beyond the stream's trimmed window.
- Any change to the transcript stream's shape or purpose.
```
