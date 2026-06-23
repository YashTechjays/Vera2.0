# Voice Lab — standalone voice-session page

**Date:** 2026-06-23
**Status:** Approved design, ready for implementation
**Implement from:** clean `main` (this spec is self-contained; it does **not** depend on
the `feat/voice-ai-pipeline-integrate` cherry-picks).

## 1. Purpose

A standalone page to initiate a voice session with the Vera agent and listen to it
live in the browser — independent of the patient-form / call-initiation flow, so it
can be built and tested in parallel while other team members finish the dependent
call-initiation work. Two session modes:

1. **In-browser** — the user talks to the bot directly through the browser mic.
2. **Outbound phone** — the backend dials an outbound phone number via a LiveKit SIP
   trunk; the callee talks to the bot over telephony.

In **both** modes the browser joins the LiveKit room so the user can **hear** the
conversation. The agent greets and converses as soon as the human/phone participant
joins.

This is a developer/QA harness. Later it will be folded into the real call-initiation
flow; for now it is deliberately decoupled.

## 2. Scope

### In scope
- A new authed page `/voice-lab`, added to the main sidebar nav.
- A new backend endpoint that creates an ephemeral LiveKit room, dispatches the
  `vera-agent` worker, optionally places an outbound SIP call, and returns a browser
  join token.
- A LiveKit-gateway method to place an outbound SIP participant (trunk id from env).
- A worker-entrypoint change so the agent **waits for the speaking participant**
  before starting (the "proper fix" for greeting timing), scoped via dispatch
  metadata so the existing `/calls` path is unchanged.

### Out of scope / non-goals
- **No persistence.** No `Call` or `PatientForm` DB rows; the session does not appear
  in Live Monitoring and has no patient-form linkage.
- **No SIP infra provisioning.** The LiveKit SIP service + outbound trunk are a
  separate (dependent) task. The outbound code path is built against a
  `trunk-id-from-env` and is **not end-to-end testable** until that infra exists. The
  in-browser path is fully testable now.
- **No transcript/recording UI.** Only live audio + connection/participant state.
- **No new agent persona.** The existing `VeraAgent` (its `_INSTRUCTIONS` + `GREETING`)
  is reused as-is.

## 3. Starting point — what clean `main` already provides

Confirmed present on `main`; reuse directly:
- `control_plane/livekit_gateway.py` — `LiveKitGateway.create_call_room(room_name)`
  (creates room + dispatches `vera-agent`) and `mint_join_token(room_name, identity)`.
- `vera_core.observability.correlation.room_name_for_call(tenant_id, call_id)` — the
  canonical room name `call--<tenant_uuid>--<call_uuid>`.
- `api/v1/calls.py` pattern (flush → `room_name_for_call` → `create_call_room` →
  `mint_join_token`) and the `LiveKit` annotated DI dep in `api/v1/common.py`.
- `settings.livekit_url` (`VERA_LIVEKIT_URL`); secrets `LIVEKIT_API_KEY` /
  `LIVEKIT_API_SECRET` via `SecretProvider`.
- Frontend `lib/api/client.ts` `apiRequest` (bearer-injecting, envelope-unwrapping).
- Agent worker `apps/agent_worker/.../main.py` (`entrypoint`, `resolve_session`) and
  `agent.py` (`VeraAgent.on_enter` → `session.say(GREETING)`).

### Must be ADDED (absent on clean main — were in the discarded cherry-pick)
- Frontend deps: `@livekit/components-react`, `@livekit/components-styles`,
  `livekit-client` (in `vera-frontend/package.json`).
- `import '@livekit/components-styles'` in `vera-frontend/src/main.tsx`.

## 4. Architecture

Three units, each independently testable.

### 4.1 Backend — `api/v1/voice_lab.py` (new router)

Endpoint: `POST /api/v1/voice-lab/sessions`, gated by `require("calls:read")`
(matching the `calls.py` interim convention).

Request DTO `StartVoiceSessionRequest` (add to `vera_core/schemas/dto.py`):
```
mode: Literal["browser", "outbound"]
phone_number: str | None = None   # required + E.164 when mode == "outbound"
```

Response DTO `VoiceSessionResponse`:
```
room_name: str
url: str        # settings.livekit_url, for the browser SDK
token: str      # browser join JWT
mode: str
```

Handler logic (no DB writes):
1. Generate a synthetic `call_uuid` (UUIDv7) → `room_name = room_name_for_call(tenant_id, call_uuid)`.
2. `await livekit.create_call_room(room_name, metadata={"wait_for_speaker": true})`
   (see §4.3 for the new optional `metadata` arg) — creates the room and dispatches
   `vera-agent`.
3. **Browser identity** depends on mode:
   - `browser`  → `caller-<user_id>`  (will publish mic)
   - `outbound` → `monitor-<user_id>` (listen-only)
4. If `mode == "outbound"`:
   - Require `settings.livekit_sip_trunk_id` — if unset, **fail closed** with a clear
     `409`/`ConflictError` ("outbound SIP not configured"). (Matches Vera's
     unset→no-capability discipline.)
   - Validate `phone_number` is E.164 (`^\+[1-9]\d{1,14}$`) → else `422`.
   - `await livekit.create_sip_participant(room_name, phone_number)` (§4.3).
5. `token = livekit.mint_join_token(room_name, browser_identity)`.
6. Return `VoiceSessionResponse`.

Register the router in `api/v1/__init__.py` (clean add — note clean `main` has **no**
`forms_router`; do not reintroduce it).

### 4.2 Settings — `vera_core/config/settings.py`

Add, following the existing fail-closed pattern next to `livekit_url`:
```
# Outbound telephony trunk for Voice Lab / SIP calls. Unset → outbound disabled
# (fail closed); the LiveKit SIP service + trunk are provisioned out of band.
livekit_sip_trunk_id: str | None = None   # VERA_LIVEKIT_SIP_TRUNK_ID
```
Document it in `vera-backend/env.example`.

### 4.3 LiveKit gateway — `control_plane/livekit_gateway.py`

- Extend `create_call_room(room_name, metadata: dict | None = None)`:
  - JSON-encode `metadata` and pass it to
    `CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name, metadata=<json>)`.
  - Default `None` → empty/no metadata → **existing `/calls` callers unchanged**.
- Add `create_sip_participant(room_name, phone_number)`:
  - Construct `api.LiveKitAPI(...)` inside the coroutine; `await lk.aclose()` in `finally`.
  - Call `lk.sip.create_sip_participant(api.CreateSIPParticipantRequest(
      sip_trunk_id=self._sip_trunk_id, sip_call_to=phone_number, room_name=room_name,
      participant_identity="phone-<call_uuid-or-sanitized-number>",
      participant_name="Outbound callee", ...))`.
  - The exact request fields are verified at implementation time against the installed
    `livekit-api` version (e.g. `wait_until_answered`, KrispEnabled, etc.).
  - `LiveKitGateway` constructor/`build_livekit_gateway` gains `sip_trunk_id`
    (from `settings.livekit_sip_trunk_id`); may be `None` when outbound is unused.

### 4.4 Agent worker — `apps/agent_worker/.../main.py` (the proper fix)

Goal: the agent must **not** greet into a room before the speaker has joined, in either
mode, without regressing the existing `/calls` dispatch.

- In `entrypoint`, after `await ctx.connect()`, parse dispatch metadata:
  `meta = json.loads(ctx.job.metadata or "{}")`.
- If `meta.get("wait_for_speaker")` is true, **before** `session.start(...)` wait for
  the first remote participant whose identity does **not** start with `monitor-`
  (covers the `caller-` browser speaker and the `phone-` SIP callee; ignores the
  listen-only monitor). Bound it with a timeout (e.g. 45–60s); on timeout, log and
  return without starting (handles a never-answered outbound call).
  - Implement as a small `wait_for_speaker(ctx, timeout)` helper using the room's
    existing participants + the `participant_connected` event. (Precise
    livekit-agents API — `ctx.wait_for_participant(kind=...)` vs an event loop — chosen
    at implementation; behavior is the contract.)
- If `wait_for_speaker` is absent/false → current immediate behavior (the existing
  `/calls` path passes no such metadata, so it is untouched).
- `VeraAgent.on_enter` already calls `session.say(GREETING)`; once the session starts
  after the speaker is present, the greeting is heard by everyone in the room
  (including the monitor in outbound mode).

### 4.5 Frontend — page, nav, api client

- `vera-frontend/src/lib/api/voiceLab.ts`:
  ```
  startVoiceSession(payload: { mode: "browser" | "outbound"; phone_number?: string })
    → POST /voice-lab/sessions → { room_name, url, token, mode }
  ```
- `vera-frontend/src/pages/VoiceLab.tsx`:
  - Controls: an E.164 phone-number input + two actions — **Start in-browser session**
    and **Start outbound call** (the latter disabled until a phone number is entered).
  - On start → `startVoiceSession` → store `{ url, token, mode }` → render a live panel
    with `@livekit/components-react` `<LiveKitRoom serverUrl url token connect
    audio={mode==="browser"} video={false}>` + `<RoomAudioRenderer/>`.
    - `browser` mode: mic enabled (`audio` true) — user speaks.
    - `outbound` mode: mic disabled (listen-only) — user monitors phone↔bot.
  - Show connection state, the participant list (agent / caller / phone), and an
    **End session** button (disconnect + tear down).
  - Error states surfaced from `ApiError` (e.g. outbound-not-configured `409`).
- Add the route in `vera-frontend/src/App.tsx` and a nav entry in
  `vera-frontend/src/lib/nav.ts`.
- Add the three LiveKit deps to `package.json` and the styles import to `main.tsx`
  (§3).

## 5. Data flow

**In-browser:**
`Click Start → POST /voice-lab/sessions {mode:"browser"} → BE: uuid→room_name→
create_call_room(meta wait_for_speaker)→ token(caller-<user>) → FE joins room (mic on)
→ worker dispatched, waits for caller-<user>, starts → agent greets → user talks, hears bot.`

**Outbound:**
`Enter phone, Click Start → POST {mode:"outbound", phone_number} → BE: require trunk env
(else 409) + validate E.164 → uuid→room_name→ create_call_room(meta wait_for_speaker)
→ create_sip_participant(trunk, phone) → token(monitor-<user>) → FE joins room
(listen-only) → phone rings; on answer phone-<…> joins → worker waits for phone- (skips
monitor-), starts → agent greets → callee converses; browser hears both sides.`

## 6. Error handling

| Condition | Result |
|---|---|
| `mode=outbound`, `livekit_sip_trunk_id` unset | `409` fail-closed, clear message |
| `mode=outbound`, `phone_number` missing/non-E.164 | `422` validation error |
| `livekit_url` unset | `build_livekit_gateway` already raises at startup |
| LiveKit room/dispatch/SIP API error | surfaced as `5xx` `ApiError`; FE shows error state |
| Outbound never answered | `wait_for_speaker` times out → worker logs + graceful shutdown; no agent greeting wasted |
| Browser denies mic (in-browser) | LiveKit surfaces a device error; FE shows it |

## 7. Testing

- **Backend** (`tests/integration/control_plane/test_voice_lab.py`, mirroring existing
  control-plane tests):
  - `browser` → `200`, returns token + `caller-` identity; `create_call_room` called
    with `wait_for_speaker` metadata (gateway mocked).
  - `outbound` with trunk unset → `409`.
  - `outbound` with invalid phone → `422`.
  - `outbound` with trunk set + valid phone → `create_sip_participant` invoked
    (gateway mocked); returns token + `monitor-` identity.
- **Worker** (`tests/unit/...`): `wait_for_speaker` ignores `monitor-` identities,
  returns on a `caller-`/`phone-` join, and times out cleanly; metadata parsing
  (absent/false → immediate path).
- **Frontend** (`vera-frontend/src/lib/api/voiceLab.test.ts`, mirroring
  `calls.test.ts`): request shape per mode; error propagation.

## 8. Open follow-ups (not this task)

- Provision the LiveKit SIP service + outbound trunk (dependent task) to make the
  outbound path testable end-to-end.
- Fold Voice Lab into the real call-initiation flow (persist a `Call`, surface in Live
  Monitoring) once that flow lands.
