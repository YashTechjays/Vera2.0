# Live Monitoring — End Call (design)

**Date:** 2026-07-11
**Status:** approved

## Problem

Voice Lab's "End session" button tears the session down for real
(`DELETE /voice-lab/sessions/{room_name}` deletes the LiveKit room, which hangs up
the SIP leg and shuts the agent down). Live Monitoring's **End Call** button
(`CallOverviewModal` footer, and `InterveneModal`) is a no-op — it only closes the
modal. There is no backend endpoint to end a real `/calls` call.

## Approach

Add `POST /calls/{call_id}/end` that authorizes the caller and deletes the call's
LiveKit room. Nothing else: the agent worker's shutdown callback emits `call.ended`,
and the existing worker-event pipeline (`close_call` → transcript finalization →
`resolve_ai_processing` → dispatch refill) drives the call to `COMPLETED` and runs
the full form lifecycle. One closeout path; the endpoint never writes call status.

Rejected alternatives:

- Reusing the Voice Lab `DELETE /voice-lab/sessions/{room_name}`: wrong permission
  gate (`voice_lab:sandbox`) and that router is deliberately decoupled from `/calls`.
- Having the endpoint call `close_call` directly: duplicates and races the
  worker-event closeout path.

## Backend (`apps/control_plane/.../api/v1/calls.py`)

- `POST /calls/{call_id}/end`, gated `require("calls:read")` plus the shared
  `_call_hidden_from` visibility rule (owner, or a published/ownerless call minus
  `revoked_user_ids`); hidden or missing call → 404, so a private call is never
  revealed.
- Already-terminal call → idempotent `ok` no-op (mirrors `publish`).
- Audit a new `AuditEvent.CALL_ENDED` record with the actor; for a non-owner,
  include the owner id in `detail` (mirrors `CALL_INTERVENE_JOIN`).
- Then `livekit.delete_room(room_name)` (already idempotent) and return
  `ok(None, "Call is ending.")`. Status transitions stay asynchronous via the
  `call.ended` worker event.
- Accepted edge: if the room is already gone but the call isn't terminal yet, the
  in-flight `call.ended` event resolves it; the endpoint stays a no-op.

## Frontend (`vera-frontend`)

- `endCall(callId)` in `src/lib/api/calls.ts` → `POST /calls/{id}/end`.
- `LiveMonitoring` owns the handler: call `endCall`, close whichever modal is open,
  optimistically drop the row from the list (the 8s poll re-syncs), surface failures
  in the page's existing error banner.
- Wire the handler into **both** End Call buttons: `CallOverviewModal` and
  `InterveneModal` gain an `onEndCall` prop with a pending/disabled state while the
  request is in flight.

## Tests

- Backend (`tests/integration/control_plane/test_calls.py`): owner ends a call;
  published non-owner ends; unpublished non-owner → 404; revoked user → 404;
  already-terminal → idempotent ok; LiveKit `delete_room` called with the call's
  room name; audit record emitted.
- Frontend (`src/lib/api/calls.test.ts` pattern): `endCall` hits the right
  path/method and unwraps the envelope.

## Decisions

- **Authorization:** same as intervene — anyone who may watch/join the call may end
  it (user-confirmed).
- **Resulting status:** `COMPLETED` via the normal pipeline; an operator-ended call
  goes through the same form lifecycle (exception review / low-completion requeue)
  as a natural hangup.
