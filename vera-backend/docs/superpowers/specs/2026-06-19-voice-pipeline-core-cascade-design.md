# Voice Pipeline — Core Cascade (Walking Skeleton) — Design

**Date:** 2026-06-19
**Status:** Draft, pending review
**Related:** ADR-0001 (cascade over speech-to-speech), ADR-0003 (self-hosted Langfuse), ADR-0005 (PHI codec retained), `vera_core.observability.correlation`, handoff "Complete the Vera Voice AI Pipeline"
**Spans two repos:** `vera-backend` (worker + control plane) and `vera-frontend` (the React SPA — separate repo at `/Users/tapusd/Work/Techjays/Vera/vera-frontend`).
**Reference POC:** `/Users/tapusd/src/tries/2026-05-18-vera-2-client-demo-cascading-vs-audio-native-model` (latency tuning + persona harvested below).

## Problem

The agent worker (`apps/agent_worker/src/agent_worker/main.py`) is a 66-line
register-join-echo skeleton with no STT/LLM/TTS. The control-plane `calls.py`
returns `ok([])`. The frontend is a dummy Vite+React SPA with no LiveKit and no
API layer. There is no path from "a patient is ready to verify" to "a live voice
agent is on a call you can watch and join from the browser."

This spec builds the **walking skeleton**: the smallest end-to-end slice that
proves the cascade voice pipeline works *through the real product UI*, while
deliberately leaving the HIPAA tokenization/encryption machinery as **inert,
well-defined seams** so it drops in later without reworking the pipeline.

## Goal & scope

Deliver a clickable end-to-end slice:

> In **Data Management**, transition a patient `READY FOR PROCESSING → IN QUEUE`.
> The control plane creates a `Call` + a LiveKit room (named via
> `room_name_for_call`) and dispatches the `vera-agent` worker into it. The call
> appears in **Live Monitoring**. Clicking **Intervene** mints a LiveKit join
> token; the browser joins the room inside `InterveneModal`, where you hear the
> agent speak its greeting, see the live transcript, and your mic plays the role
> of the payer representative (there is no SIP/telephony leg yet).

That exercises Deepgram→Gemini→Cartesia end-to-end with production-grade latency
tuning, driven by the real frontend.

**In scope:** agent-worker cascade; minimal control-plane LiveKit orchestration
(create-call + dispatch, join-token, list); frontend LiveKit integration in the
existing modal flow; LiveKit in local docker-compose; inert PHI/transcript seams;
correlation/observability wiring (consume `correlation.py`, no new key).

**Out of scope (seams left ready):** PHI tokenization / codec, any encryption,
the Redis live-transcript stream + Postgres transcript persistence, **SIP /
telephony (the payer leg)**, supervisor whisper/takeover, recording-to-GCS,
transcript→form extraction, and the real patient-data backend (the IBV form in
`InterveneModal` stays dummy). The agent has **no tools** in this slice
(chat-only — see §A.3).

## Non-goals / deferred decisions

- **No tokenization now.** The wall is structurally present (three Agent node
  overrides routing through a `PHIBoundaryProtocol`) but resolves to a no-op
  `PassthroughPHIBoundary`. Raw text flows STT→LLM→TTS. This slice must run on
  **synthetic / role-play data only** — no real PHI on a call until the codec is
  wired (a later spec) — because nothing de-identifies yet.
- **No encryption, no Redis transcript stream, no Postgres transcript persistence.**
  Live transcript is shown via LiveKit's built-in transcription forwarding only.
- **No app-level column encryption** (deferred repo-wide per `vera_core/CLAUDE.md`).

## End-to-end architecture

```
 Data Management (frontend)                         Live Monitoring (frontend)
   IN QUEUE transition                                 active call list / Intervene
          │                                                     │
          ▼  POST /v1/calls                                     ▼  GET /v1/calls/{id}/join-token
 ┌─────────────────────────── control_plane (FastAPI) ───────────────────────────┐
 │  create Call row → room = room_name_for_call(tenant_id, call.id)               │
 │  LiveKit: create room + agent_dispatch.create_dispatch("vera-agent", room)     │
 │  mint AccessToken(room_join, identity=supervisor) for the browser              │
 └───────────────────────────────────────────────────────────────────────────────┘
          │ dispatch                                            │ token + LIVEKIT_URL
          ▼                                                     ▼
 ┌──────────── agent_worker (LiveKit) ────────────┐   ┌──── browser (InterveneModal) ────┐
 │ Deepgram(Flux) → Gemini → Cartesia              │   │ LiveKitRoom + RoomAudioRenderer  │
 │ VeraAgent: stt_node / transcription_node /      │◄─►│ mic publish (Audio btn)          │
 │   tts_node  → PassthroughPHIBoundary (no-op)    │   │ Live Transcripts tab             │
 │ greeting on_enter; correlation span tags        │   └──────────────────────────────────┘
 └─────────────────────────────────────────────────┘
                         LiveKit server (docker-compose, --dev)
```

## A. Agent worker (`apps/agent_worker`) — the core

### A.1 Dependencies (`pyproject.toml`)
Add the LiveKit plugin extras + Silero/turn-detector (mirror the POC):
`livekit-agents[deepgram,google,cartesia,silero,turn-detector]>=1.5.17`.
Vendor keys (Deepgram/Cartesia/Google) and `LIVEKIT_*` resolve via the existing
`EnvSecretProvider` / `.env` locally — **no new plaintext Settings fields** for
secrets (per `settings.py` docstring).

### A.2 Cascade (`agent_worker/cascade.py` — `build_session(...)`)
Wires the providers and the latency knobs harvested from the POC (the single
biggest wins are `preemptive_generation` fed by Deepgram Flux's eager EOT, and
Gemini `thinking_budget=0`):

| Stage | Choice | Notes |
|---|---|---|
| STT | `deepgram.STTv2(model="flux-general-en", eager_eot_threshold=0.5)` | Flux gives a sharp preemptive-generation trigger; STTv2 has no `smart_format`/`interim_results` |
| LLM | `google.LLM(model="gemini-2.5-flash", thinking_config=ThinkingConfig(thinking_budget=0))` | thinking off = the key TTFT win |
| TTS | `cartesia.TTS(model="sonic-3.5", voice=…, emotion="confident")` | only honored generation knob on sonic-3.5 |
| VAD | Silero, **prewarmed** (`min_silence_duration=0.4`) | shaves ~150ms off turn end |
| Turn detection | `EnglishModel` (turn-detector plugin) | **keep it** — dropping it falls back to dumb VAD-silence, which is worse |

`AgentSession(...)` knobs: `preemptive_generation=True`, `min_endpointing_delay=0.3`,
`max_endpointing_delay=0.6`, `allow_interruptions=True`, `min_interruption_duration=0.5`,
`false_interruption_timeout=2.0`, `resume_false_interruption=True`. (These emit v2.0
deprecation warnings in 1.5.x but still work — do not "fix" until the 2.x bump.)
`vad`/`turn_detection` are injectable so the prewarmed VAD is passed in.

Provider-vs-fake selection lives here (see §F testing).

### A.3 `VeraAgent(Agent)` (`agent_worker/agent.py`) — the inert seams
Subclass with the three override points the POC validated as the correct PHI
placements (so later integration is literally swapping `PassthroughPHIBoundary`
for the real `PHIBoundary`):

- `stt_node` → wraps the default node; on FINAL/PREFLIGHT transcripts calls
  `await boundary.redact(session_id, text)` (**identity now**). This is the
  preemptive-generation-safe redact seam (NOT `on_user_turn_completed`, which
  misses PREFLIGHT). No interim captions (confirmed not needed).
- `transcription_node` → pass-through (**no-op tap now**); later emits tokenized
  segments to the transcript stream and stays tokenized in UI/DB.
- `tts_node` → wraps default; runs text through `SpeechStreamHydrator` /
  `boundary.hydrate_for_speech` (**identity now**) — audio-only hydration later.

**Prompt:** the POC `SYSTEM_PROMPT` + `GREETING` (verbatim) **+ `CARTESIA_MARKUP_GUIDE`**
(we use sonic-3.5, so the `<spell>`/`<break>` code-readback guide is included).
**Adapted for chat-only:** strip the `TOOL USE` section and all
`record_service_coverage` / `end_call` references; the agent verbally verifies
coverage and speaks a polite closing line when done (no tool to end the call —
the supervisor ends it via the UI, or the session closes). `tools=[]`.
Greeting spoken on `on_enter` via `session.say(GREETING)`.

### A.4 Lifecycle (`agent_worker/main.py`)
`entrypoint(ctx)`: `parse_room_name(ctx.room.name) → RoomRef` (reuse correlation,
do not reinvent); `build_phi_boundary(settings)` → `open_session` (no-op);
`build_session(...)`; `session.start(VeraAgent(...), room=ctx.room)`; tag spans
with `call_trace_attributes(room_name)`. Shutdown via
`ctx.add_shutdown_callback` → `boundary.close_session` (no-op) + graceful close.
End-call/closing follows the framework-blessed `speech_handle` done-callback →
`session.shutdown(drain=True)` pattern (never inline `aclose()` — it drops the
goodbye line).

### A.5 Prewarm (`prewarm_fnc`)
Load Silero VAD once per process (`proc.userdata["vad"]`); pre-download the
turn-detector ONNX (`python -m livekit.agents download-files`). Raise
`initialize_process_timeout` to leave headroom for the future codec cold-load.

## B. Inert PHI/transcript seam (`vera_core/phi/`) — structural half of handoff #2

- `PHIBoundaryProtocol` — a `Protocol` over the existing `PHIBoundary` shape:
  `open_session`, `close_session`, `redact`, `hydrate_for_speech`, `hydrate_raw`.
- `PassthroughPHIBoundary` — no-op impl: `redact`/`hydrate_*` return input
  unchanged; `open/close` are no-ops.
- `build_phi_boundary(settings, …) -> PHIBoundaryProtocol` — factory mirroring
  `build_kms`; **returns `PassthroughPHIBoundary` for now.** The cascade depends
  only on the Protocol. Later: the real `PHIBoundary` + `phi_tokenizer_disabled`
  flag + prod hard-fail guard land behind this same factory with **zero cascade
  changes**.

## C. Control plane (`apps/control_plane`) — minimal LiveKit orchestration

Add the LiveKit server SDK dep (`livekit-api`) and `LIVEKIT_URL/API_KEY/API_SECRET`
settings (env-routed). Three endpoints under the existing tenant-scoped authz
chain (`require("calls:read")` / a `calls:write` for create):

1. **`POST /v1/calls`** — body: patient/form ref. Creates a `Call` row
   (`form_id`, `tenant_id`, `current_status`), computes
   `room = room_name_for_call(tenant_id, call.id)`, calls LiveKit
   `create_room` + `agent_dispatch.create_dispatch(agent_name="vera-agent", room=room)`,
   writes a `CallEvent`, returns the (grown) `CallSummary`. Triggered by the
   `IN QUEUE` transition.
2. **`GET /v1/calls/{call_id}/join-token`** — verifies the call belongs to the
   caller's tenant; mints a LiveKit `AccessToken` (grant `room_join` scoped to
   that room, supervisor identity); returns `{ token, url }`. `call_id` in the
   path is a UUID (non-PHI) — fine.
3. **`GET /v1/calls`** — replaces `ok([])`; lists active calls for Live
   Monitoring (tenant-scoped).

Grow `CallSummary` (`vera_core/schemas/dto.py`) with: `status`, `room_name`,
`patient_name`/`insurance` (from the joined form — dummy-safe for now),
`started_at`. Wire `configure_observability()` into the control-plane lifespan
(`main.py:84` TODO) so spans export when `VERA_LANGFUSE_HOST` is set (no-op
otherwise).

## D. Frontend (`vera-frontend`, separate repo) — real LiveKit integration

- **Deps:** add `livekit-client` + `@livekit/components-react`
  (+ `@livekit/components-styles`). None exist today.
- **API layer:** create `src/lib/api/calls.ts` mirroring the `mock.ts`
  swap-point convention (`VITE_API_URL`): `startCall(formId)`,
  `getJoinToken(callId)`, `listActiveCalls()`. (No API layer exists today; this
  is the seam where real backend calls live.)
- **Data Management** (`RecordFormModal`, `DataManagement.tsx`): the
  `READY FOR PROCESSING → IN QUEUE` status change calls `startCall(formId)`
  (in addition to the existing local-state update).
- **Live Monitoring** (`LiveMonitoring.tsx`): the active-call list reads
  `listActiveCalls()`; **Intervene** chain (table → `CallOverviewModal` → its
  Intervene → `LiveMonitoring` handler) calls `getJoinToken(callId)` then opens
  `InterveneModal` with `{ token, url, roomName }`. Extend the `LiveCall` type
  with `callId`/`roomName` (absent today).
- **`InterveneModal`**: replace the dead "Connecting to call…" placeholder with
  a `LiveKitRoom` (token+url) + `RoomAudioRenderer`; the bottom-left **Audio**
  button toggles supervisor mic publish; the **Live Transcripts** tab renders
  LiveKit transcription events. **Patient Information** tab + IBV form stay dummy.
  *Implementation note for the plan: confirm the exact transcription-rendering
  hook for the pinned `@livekit/components-react` version (text-stream
  `lk.transcription` vs `useTranscriptions`).*

## E. Local infra (`docker-compose.yml`)

Add a `livekit` service:
`livekit/livekit-server --dev --bind 0.0.0.0 --node-ip 127.0.0.1`, ports
`7880` (HTTP/WS), `7881` (TCP), `7882/udp` (RTC). `--bind 0.0.0.0` is required or
the Docker port-forward can't reach it; `--node-ip 127.0.0.1` or ICE hangs in
"checking" forever from the browser. (Langfuse stays out of compose — `otel.py`
no-ops without `VERA_LANGFUSE_HOST`.)

## Data flow (one turn, today)

mic (supervisor as payer rep) → Deepgram → `stt_node`(identity) →
Gemini(preemptive) → `transcription_node`(passthrough → client transcript) →
`tts_node`(identity) → Cartesia → browser audio. Every arrow that will later
cross the PHI wall already routes through a seam.

## Correlation & observability

Reuse `vera_core.observability.correlation` verbatim — the room name is the one
correlation key across LiveKit, Langfuse, and (later) Redis. Control plane names
the room with `room_name_for_call`; the worker recovers identity with
`parse_room_name` and tags every span with `call_trace_attributes(room_name)`
(which sets `langfuse.session.id`). No new identifier scheme.

## F. Testing strategy

- **CI (`just check`):** fake STT/LLM/TTS doubles (no real vendors, no network,
  deterministic) assert wiring, seam pass-through, lifecycle open/close, and the
  control-plane endpoints (room-name derivation, dispatch call shape, token
  grant scope). Frontend: `vitest` for the `calls.ts` client + the Intervene
  handler wiring. `ruff` + `mypy --strict` clean.
- **Manual (the real validation):** `just up` (compose incl. LiveKit) + `just api`
  + `just worker` + `npm run dev` (frontend). Walk the UI: IN QUEUE → Live
  Monitoring → Intervene → hear the greeting, talk to the agent as the payer rep,
  watch the live transcript. Requires real Deepgram/Cartesia/Google + `LIVEKIT_*`
  keys in `.env`.

## Acceptance criteria

1. `just check` green (backend); frontend `lint` + `test` green.
2. `just up` starts LiveKit; worker registers as `vera-agent` (explicit dispatch).
3. IN QUEUE transition creates a `Call` + room + dispatch; the call shows in Live
   Monitoring.
4. Intervene connects the browser to the room; the agent speaks the greeting; a
   spoken exchange works end-to-end with the POC persona; live transcript renders.
5. Perceived turn latency is in the POC's ~1.0–1.6s cascading band (region
   caveat: providers are US-hosted).
6. The three Agent seams are present and pass-through; swapping
   `build_phi_boundary` to a real boundary would require no cascade edits.

## Open questions / compliance & devops-todo rows to add

- `adr/devops-todo.md`: provisioning rows for LiveKit (prod), Deepgram, Cartesia,
  Vertex/Gemini, and `LIVEKIT_*` secret storage (only NTP-clock + Cloud-KMS rows
  exist today).
- **Synthetic-data-only guardrail:** until the codec is wired, document loudly
  that this pipeline must not carry real PHI (no de-identification yet).
- Frontend↔backend auth: the join-token endpoint runs under the tenant authz
  chain; the frontend currently has no auth/session — the plan must decide how
  the SPA authenticates to the control plane (likely deferred to a dummy/dev
  token initially, flagged for the real auth integration).

## Risks

- **livekit-agents 1.5.x API drift / deprecations** — pin the version; expect the
  `min/max_endpointing_delay` + `preemptive_generation` deprecation warnings.
- **Frontend transcription API** — the exact hook varies by
  `@livekit/components-react` version; confirm during planning.
- **No payer leg** — "the supervisor plays the rep" is a test affordance; real
  call behavior (agent-initiated outbound, rep on the line) only arrives with the
  SIP slice. The persona greeting assumes it called *out* to a rep.
- **Cross-repo coordination** — backend and frontend ship together for the slice
  to be demoable; the spec doc lives in `vera-backend` but the plan must track
  the `vera-frontend` changes explicitly.
