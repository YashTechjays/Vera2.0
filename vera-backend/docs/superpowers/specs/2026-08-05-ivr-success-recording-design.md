# IVR Success recording (VR2-45 Step 4, metric 1 of 2)

**Date:** 2026-08-05
**Status:** Design — approved direction
**Repos touched:** `vera-backend` only

## Problem

The analytics plan (VR2-44/45) defines an IVR Success metric: of the calls where
the IVR navigator ran, what percentage reached a live human? Nothing records this
today — the handoff moment exists in the worker (`ivr_agent.py::
transfer_to_verification`, reason `ivr_live_human`) but produces only a log line
and an OTel span tag, neither of which is a reporting store. The metric cannot be
backfilled, so recording ships first; the dashboard card comes in a later PR once
data has accumulated.

## Definition (agreed)

- **Denominator:** calls where the IVR navigator actually ran (the form's
  `ivr_navigation_enabled` toggle was on) AND the callee answered
  (`Call.started_at IS NOT NULL`). Calls with the toggle off, and calls that never
  connected (busy / no-answer / trunk failure), are excluded entirely.
- **Numerator:** of those, calls where the navigator exited the IVR task by
  handing off to the verification agent.
- **Cancelled rule:** a call cancelled by a supervisor/user while still inside the
  IVR task is excluded from the denominator — deliberate cancellation is neither
  success nor failure.

## Key facts (verified against the code)

- The worker publishes lifecycle events to the control plane over a Redis stream:
  a discriminated Pydantic union in `vera_core/events/worker.py`
  (`call.failed | call.answered | call.ended | call.answer_recorded | call.health`),
  consumed by `control_plane/worker_events.py`. The worker has no DB access.
- The queue dispatcher creates the `Call` row and builds the dispatch metadata,
  including `metadata["enable_ivr_navigation"]`
  (`vera_core/services/queue_dispatcher.py:368`), sourced from
  `PatientForm.ivr_navigation_enabled`.
- Voice Lab sessions set the same metadata flag (`api/v1/voice_lab.py:151`) but do
  not create patient `Call` rows, so they never enter the metric.
- `CallStatus.CANCELED` is a distinct terminal status (user-requested end), so the
  cancelled rule is expressible in SQL with no new state.
- Repo precedent for reporting fields is first-class columns on `call`, frozen at
  event time (`completion_pct` via `call_lifecycle.py:48`; the `oversight.py`
  comment "first-class column so the report is a GROUP BY, not a JSONB scan").

## Design

### 1. Schema — two columns on `call` (one migration)

- `ivr_enabled BOOLEAN NOT NULL DEFAULT FALSE` — the navigator ran on this call.
- `ivr_exited_at TIMESTAMPTZ NULL` — when the navigator reached a human;
  `NULL` = it never did.

Migration must be idempotent (`ADD COLUMN IF NOT EXISTS`) because migration 0001
materializes DDL from live models; revision id is alembic-generated, chain kept
linear off the dev head (per repo migration rules).

### 2. Denominator stamp — control plane, at call creation

In the dispatcher, where the `Call` row is created and
`metadata["enable_ivr_navigation"]` is set, stamp the same flag onto the row:
`call.ivr_enabled = <flag>`. No worker involvement; the flag is immutable for the
life of the call.

### 3. Numerator — one new worker event

`IvrExitedEvent` added to the union in `vera_core/events/worker.py`:

```python
class IvrExitedEvent(BaseModel):
    """Emitted when the IVR navigator hands off to the verification agent —
    the navigator reached a live human."""
    type: Literal["ivr.exited"] = "ivr.exited"
    room_name: str
    ts: int  # epoch milliseconds
```

Published from `ivr_agent.py::transfer_to_verification`, immediately after the
handoff decision (same publish mechanism the worker uses for its other lifecycle
events). Payload carries no PHI — room name (tenant/call UUIDs) and timestamp only.

### 4. Consumer — one new handler in `worker_events.py`

`_handle_ivr_exited`:
1. `parse_room_name` → `None` ⇒ skip (foreign/console room, existing behavior).
2. Load the `Call` in a tenant session; missing row ⇒ existing
   retry-young-or-drop helper (event raced the Call commit).
3. If `call.ivr_exited_at` is `NULL`, set it from the event `ts`; otherwise no-op
   (idempotent under Redis redelivery, matching the other handlers).

### 5. The metric (documented now, dashboard card later)

```sql
ivr_success_pct =
  COUNT(*) FILTER (WHERE ivr_exited_at IS NOT NULL)
  / COUNT(*) FILTER (
      WHERE ivr_enabled
        AND started_at IS NOT NULL
        AND NOT (current_status = 'canceled' AND ivr_exited_at IS NULL))
```

Numerator rows are always a subset of denominator rows (an exited call is never
excluded). Lives in `api/v1/analytics.py` when the card ships; until then this
spec is the definition of record.

## Error handling

- **Event before Call commit:** retry-young-or-drop (existing helper).
- **Worker crash between handoff and publish:** the call reads as an IVR failure.
  Rare, and biased conservatively — the metric never over-reports success.
- **Redelivery:** no-op via the NULL-guard.
- **Unknown event type on old control planes:** `parse_worker_event` raises on
  unknown types — deploy order is control plane before (or with) worker, same
  constraint every previously added event type had.

## Testing

- Unit (`test_worker_events.py` pattern): handler stamps `ivr_exited_at`; second
  delivery is a no-op; foreign room skipped; missing Call retried-young.
- Unit (dispatcher): `ivr_enabled` stamped true/false from the form flag.
- Unit (worker): `transfer_to_verification` publishes `ivr.exited` for the room.
- Integration: seeded calls covering all four cells (exited / not-exited ×
  enabled / disabled) plus a cancelled-in-IVR call; assert the documented SQL
  returns the expected percentage and the cancelled call is excluded.

## Out of scope

- The dashboard card / analytics endpoint change (later PR, after data accumulates).
- Cost Per Call recording (separate design, next).
- Backfill (impossible by definition).
- Recording IVR *entry* time or per-menu-step telemetry (YAGNI — the enabled flag
  plus answered status already define "the navigator ran").
