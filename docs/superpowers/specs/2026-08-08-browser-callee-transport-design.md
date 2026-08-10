# Browser-callee transport — join a real call as the payer rep, without SIP

**Date:** 2026-08-08
**Status:** Approved (design)
**Branch:** `bypass-outbound-call-for-speed-testing`

## Problem

Testing a voice-path change end to end today requires a live Twilio call. That path is
currently unreliable in dev — calls end on their own and the inbound audio stream replays —
which blocks verification of work that has nothing to do with telephony (the prompt-compiler
branch being the immediate case).

The telephony defect is real and is **out of scope here**. This design removes the dependency
on it for testing, so voice-path work can be verified while it is investigated separately.

## Goal

Run the **entire** production call lifecycle — send a patient form to the queue, dispatch,
compile and stage the CallPlan, create the room, dispatch the agent worker, answer, converse,
extract answers, close out — with exactly one substitution: **the payer rep is a browser
participant instead of a SIP callee.**

The tester opens Live Monitoring, finds the call, and joins as the participant the outbound
call would have reached.

### Non-goals

- Fixing the Twilio/SIP defect.
- Any capability that can exist in production. The switch is off by default and the join
  endpoint refuses to mint a token when it is off.
- Simulating ringing, busy, no-answer, or carrier failure. Those are SIP-layer states; this
  transport has none of them.

## Why this is small

The mechanism already exists and is already exercised — it just isn't reachable from `/calls`:

- The worker already accepts a browser participant as the call's speaker.
  `_is_ready_speaker` (`agent_worker/main.py:93`) returns `True` for any non-observer,
  non-SIP participant. This is the Voice Lab "browser mode" path.
- `LiveKitGateway.mint_join_token(..., can_publish=True)` already exists
  (`livekit_gateway.py:329`).
- `LiveCallRoom` already publishes the local mic under `microphone={true}`
  (`monitoring/LiveCallRoom.tsx:356`).
- `ACTIVE_CALL_STATUSES` already includes `INITIATED` (`api/v1/calls.py:108`), so a
  dispatched-but-undialed call is already visible in Live Monitoring — there is something to
  click on before anyone has joined.
- Teardown already works. `RoomInputOptions(close_on_disconnect=True,
  delete_room_on_close=True)` (`main.py:236`) closes the session and deletes the room when the
  linked speaker leaves — a browser tab closing ends the call exactly as a phone hangup does.

Ten files change, most by a few lines each. No migration, no schema change, no new DTO on a
production surface:

| File | Change |
|---|---|
| `vera_core/config/settings.py` | the flag |
| `control_plane/livekit_gateway.py` | carry it on the gateway |
| `control_plane/queueability.py` | skip the trunk check |
| `control_plane/api/v1/patient_forms.py` | pass the gateway to `ensure_queueable` |
| `vera_core/services/queue_dispatcher.py` | skip trunk lookup + dial; set metadata |
| `control_plane/api/v1/calls.py` | the `callee=true` join mode |
| `vera_core/models/audit_log.py` | `CALL_CALLEE_JOIN` |
| `agent_worker/main.py` | fire `answered` for a browser callee |
| `monitoring/LiveCallModal.tsx` | the button |
| `lib/api/calls.ts` | the `callee` query parameter |

## Design

### 1. The switch

```python
# vera_core/config/settings.py
browser_callee_transport: bool = False   # VERA_BROWSER_CALLEE_TRANSPORT
```

The flag rides on **`LiveKitGateway`**, set once in `build_livekit_gateway(settings, secrets)`
and exposed as a read-only property.

Rationale: the two places that must consult it (the enqueue gate and the dispatcher) already
receive the gateway by injection, and "does this deployment dial SIP or wait for a browser
callee" is a property of the transport. Threading a keyword argument instead would touch
`dispatch.py`'s three functions and all four of its call sites (`patient_forms.py:1515`,
`calls.py:820`, `worker_events.py:799`, `pipeline_sweeper.py:275`) for a value none of them
have an opinion about. With the gateway carrying it, those files are untouched.

`try_dispatch` types its gateway as `Any` (duck-typed, so tests can pass fakes), so the
property read needs no signature change there. Existing dispatcher fakes gain the attribute.

The frontend gets its own `VITE_BROWSER_CALLEE_TRANSPORT`, matching the established
`VITE_DEV_EMAIL` / `VITE_DEV_PASSWORD` dev-affordance convention (`pages/Login.tsx:12`). It
gates button *visibility* only; the backend flag is the authority. If the two drift, the join
endpoint returns a 409 with a clear message rather than misbehaving.

### 2. Enqueue gate — `control_plane/queueability.py`

`ensure_queueable` currently rejects a form that could never be dialed: no valid E.164 payer
phone number, or no outbound SIP trunk configured for the tenant (`queueability.py:36-47`).

Under browser-callee transport, skip **only** the trunk check. The E.164 requirement stays —
a test form should carry realistic data, and the intake validation being exercised is
unrelated to transport.

The endpoint (`patient_forms.py:1427`) already has the `LiveKit` dependency available to pass
in.

### 3. Dispatcher — `vera_core/services/queue_dispatcher.py`

Two edits, both narrow:

- **`:309-321`** — the per-pass trunk resolution. Today a missing trunk blanks `candidates`
  entirely, so nothing dispatches. Skip the lookup under browser-callee transport.
- **`:528-562`** — the dial. Skip `create_sip_participant` and the `dial_pacing_s` sleep that
  paces carrier CPS. With no dial there is no `OutboundDialError` branch to take, so the
  call proceeds directly to the post-dial accounting.

Everything between those two points is **unchanged**: the CallPlan compile
(`_resolve_plan_template`), the per-form prefill fuse (`_resolve_call_plan`), the retry
focusing, `plan_service.put`, `create_call_room`, the `INITIATED` `CallEvent`, and
`CallLineage`. That is the code path the prompt-compiler work lives in, and it must stay
byte-identical for the test to mean anything.

`start_recording_for_call` still fires (full-fidelity decision below). It is a no-op locally
when no `RecordingConfig` is configured, and it is already fail-open.

### 4. Join token — `control_plane/api/v1/calls.py`

A third join mode alongside listen-only and intervene:

```
GET /calls/{call_id}/join-token?callee=true
```

A boolean query parameter, mirroring the existing `intervene: bool = False`. (`as` was
considered and rejected — it is a Python keyword and cannot name a parameter.) `callee=true`
and `intervene=true` together is a 422; they are mutually exclusive roles.

The minted token:

| Property | Value | Why |
|---|---|---|
| identity | `caller-{user_id}` | Non-observer, so `is_observer_identity` is `False` and `_is_ready_speaker` accepts it |
| `can_publish` | `True` | The callee speaks |
| `vera.mode` attribute | **absent** | **Critical.** Any value here trips `AgentTakeoverController` (`main.py:800`) and permanently silences the agent — you would join to a bot that never speaks |
| intervener lock | **not claimed** | The callee is not a supervisor. No `call.intervener_user_id` write, no `InterventionEvent` row |
| TTL | `_LISTEN_TOKEN_TTL` | The intervene grace exists to bound a stolen lock; there is no lock here |

Authorization reuses the existing chain: `require("calls:read")`, then the `_call_hidden_from`
visibility 404 (so a private call never becomes a 403). It additionally returns
`CONFLICT` when `browser_callee_transport` is off — this endpoint cannot mint a
publish-capable non-supervisor token in production.

A publish-capable join is a distinct disclosure from a listen-only one and gets its own audit
event rather than being folded into `CALL_LISTEN_ONLY_JOIN`:

```python
CALL_CALLEE_JOIN = "call.callee.join"   # AuditEvent, models/audit_log.py
```

`audit_log.event_type` is `String(64)` (`audit_log.py:98`), so this needs no migration.

### 5. Worker — `agent_worker/main.py`

One condition. At `:399`, `lifecycle.answered()` fires only when the resolved speaker is
`ParticipantKind.SIP` — correct today, because a Voice Lab browser caller is not an answered
phone call. Under browser-callee transport it must also fire for a browser speaker.

The dispatcher signals this in the room metadata it already builds (`queue_dispatcher.py:364`):

```python
metadata["browser_callee"] = True
```

This single line carries the call `INITIATED → ACTIVE`, which is what makes everything
downstream behave like production: `CallHealthObserver` status promotion, the End Call
endpoint's `live` branch rather than `pre_answer` (`calls.py:794`), the `IN_CALL →
AI_PROCESSING` form transition, and recording lifecycle.

`_SPEAKER_TIMEOUT_S` stays at **60 seconds**, unchanged. The tester is the one clicking, and
60s from enqueue to join is comfortable. Missing the window produces a normal `call.failed` /
no-answer outcome, which is a legitimate state to observe.

### 6. Frontend — `components/monitoring/LiveCallModal.tsx`

A **"Join as payer rep"** button beside Intervene, rendered only when
`VITE_BROWSER_CALLEE_TRANSPORT` is set.

It reuses `LiveCallRoom` with `microphone={true}` — the mic-publishing path already exists and
is already covered by tests. The mode becomes part of the component `key`, exactly as
intervene already is (`LiveCallModal.tsx:131`), because LiveKit ignores a token swap while
connected and the room must remount.

Nothing else in the frontend changes. Teardown is already correct.

## Data flow

```
Send to queue (browser transport on)
  └─ ensure_queueable: E.164 checked, trunk check skipped
  └─ IN_QUEUE, dispatch pass scheduled post-commit

try_dispatch
  ├─ trunk lookup ........................ SKIPPED
  ├─ compile CallPlan + fuse prefill ..... unchanged
  ├─ plan_service.put(room, plan) ........ unchanged
  ├─ create_call_room(metadata + browser_callee=True)
  ├─ Call row INITIATED, CallEvent ....... unchanged
  └─ create_sip_participant .............. SKIPPED

Live Monitoring (call visible: INITIATED ∈ ACTIVE_CALL_STATUSES)
  └─ "Join as payer rep" → join-token?callee=true
       └─ caller-{user_id}, can_publish, no vera.mode

Worker: wait_for_speaker resolves (browser participant, ≤60s)
  └─ lifecycle.answered()  →  call ACTIVE
  └─ agent greets; conversation runs the compiled plan

Tester closes the tab
  └─ close_on_disconnect → session closes → delete_room_on_close
  └─ call.ended → normal closeout
```

## Testing

- **Unit, dispatcher:** with a fake gateway carrying `browser_callee_transport=True` and no
  trunk credential, `try_dispatch` creates the room, stages the plan, and returns a
  dispatched count without calling `create_sip_participant`. The inverse — flag off, no trunk
  — still blanks candidates.
- **Unit, queueability:** `ensure_queueable` passes with no trunk under the flag, and still
  rejects a missing/invalid E.164 number under it.
- **Unit, worker:** `lifecycle.answered()` fires for a non-SIP speaker when metadata carries
  `browser_callee`, and does not when it doesn't (protecting the Voice Lab browser-mode
  contract).
- **Integration, join token:** `callee=true` mints a `caller-` identity with `can_publish` and
  **no** `vera.mode` attribute; returns 409 when the flag is off; 422 with `intervene=true`;
  claims no intervener lock; emits `CALL_CALLEE_JOIN`.
- **Frontend:** the button renders only under the env var; the mode participates in the room
  `key`.
- **Manual, the whole point:** boot `just up` / `just api` / `just worker` with the flag set,
  send a form to queue, join from Live Monitoring, hold a conversation, confirm `field_answer`
  rows land and the form reaches `AI_PROCESSING`.

`just check` must pass on the final tree.

## Risks

**A publish-capable token outside the supervisor model.** Mitigated by three independent
gates: the setting defaults to `False`, the endpoint 409s when it is off, and the existing
`calls:read` + visibility 404 chain still applies. The identity is `caller-`, which carries no
supervisor privileges anywhere in the codebase.

**Divergence from real calls.** The transport genuinely differs — no ringing, no carrier
failure modes, no SIP audio characteristics, no DTMF over the trunk, and no IVR to navigate.
This harness verifies the *conversation and form-filling* path, not telephony. A live call
remains required before shipping voice-path changes, per `vera-backend/CLAUDE.md`.

**Flag drift between backend and frontend.** The button could appear with the backend flag
off. Surfaces as a clear 409 on click, not a broken call.

**PHI.** No new PHI surface. The room name still carries tenant/call UUIDs only, dispatch
metadata is unchanged apart from one boolean, and the join is audited like every other.
