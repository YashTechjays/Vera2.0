---
name: livekit
description: Vera's LiveKit conventions — self-hosted OSS voice pipeline (control-plane room/token gateway + agent-worker Deepgram→Gemini→Cartesia cascade). Use when creating/joining rooms, dispatching the agent worker, touching the AgentSession/turn-handling config, or LiveKit settings/secrets.
---

# LiveKit (Vera voice pipeline)

Vera runs **self-hosted LiveKit OSS only — never LiveKit Cloud** (CLAUDE.md trust
boundary). The pipeline spans two processes that never share state: the FastAPI
**control plane** (creates rooms, dispatches the worker, mints browser join tokens)
and the **agent worker** (the Deepgram→Gemini→Cartesia cascade that joins the room).

## Bright lines — NEVER

- **NEVER use `livekit.agents.inference.*`** — those run on LiveKit's hosted gateway
  (`agent-gateway.livekit.cloud`), which we don't have. They stream call audio (PHI)
  off-box. Every audio/turn model must be a local plugin model.
- **NEVER let interruption ("barge-in") auto-select** — left to auto-detect it picks the
  Cloud-only *adaptive* ML detector (401s against the gateway, streams audio off-box).
  Pin `interruption.mode="vad"` (local Silero VAD). See `apps/agent_worker/.../cascade.py`.
- **NEVER put the API secret in plaintext config or logs** — it lets an attacker create
  rooms, join calls, and eavesdrop (`adr/devops-todo.md` #3).

## Room name IS the correlation key

One verification call = one room. The room name is the session id AND the Langfuse
`langfuse.session.id` — both processes derive identical trace ids from it without shared
state. Canonical form: `call--<tenant_uuid>--<call_uuid>`.

- Build/parse only via `vera_core.observability.correlation`:
  `room_name_for_call(tenant_id, call_id)` / `parse_room_name(...)` (→ `RoomRef | None`).
- A foreign room name (`parse_room_name` → None) runs **only** in local dev (the
  `lk console`/`connect` mic test); in any non-local env the worker rejects it and never
  attaches to a room it wasn't dispatched to (`main.resolve_session`).
- Room name carries tenant/call UUIDs — fine for spans; raw PHI never goes in a room name.

## Control plane — the gateway (`control_plane/livekit_gateway.py`)

`LiveKitGateway` wraps the server SDK. Follow these exactly:

- Construct `api.LiveKitAPI(...)` **inside the coroutine**, not `__init__` — it opens an
  aiohttp session that needs a running loop. Always `await lk.aclose()` in `finally`.
- `create_call_room(room_name)` creates the room **and** dispatches the worker via
  `agent_dispatch.create_dispatch(CreateAgentDispatchRequest(agent_name=AGENT_NAME, ...))`.
  Dispatch is **explicit only** (`AGENT_NAME = "vera-agent"` on both sides) — the worker
  never auto-joins.
- `mint_join_token(room_name, identity)` → JWT with `VideoGrants(room_join=True, room=...)`.
- Build via `build_livekit_gateway(settings, secrets)`; it raises if `livekit_url` is unset.
- Inject into endpoints with the `LiveKit` annotated dep (`api/v1/common.py` →
  `get_livekit`, reads `app.state.livekit`). Never construct a gateway in a handler.
- Usage pattern in `api/v1/calls.py`: flush the `Call` to get its UUIDv7 → `room_name_for_call`
  → `create_call_room` → later `mint_join_token` for a `supervisor-{user_id}` identity.

## Agent worker — the cascade (`apps/agent_worker/...`)

- **Entrypoint** (`main.py`): `await ctx.connect()` → resolve session id from room name →
  set OTel attrs (`call_trace_attributes`) → `build_phi_boundary` + `open_session` →
  `build_session()` → register a shutdown callback that `close_session`s the boundary →
  `session.start(agent=VeraAgent(...), room=ctx.room)`. VAD is built once in `prewarm` and
  stashed in `proc.userdata`.
- **Session config** (`cascade.py`): STT `deepgram.STTv2(model="flux-general-en", eager_eot...)`,
  LLM `google.LLM(model=<resolved>, vertexai=True, location="global", thinking_config=...)`,
  TTS `cartesia.TTS(model="sonic-3.5", ...)`, local `silero.VAD`, turn detection `EnglishModel()`.
  The model is NOT hardcoded — `resolve_llm_model` takes the runtime override else
  `Settings.voice_llm_default_model`. Thinking is whichever key that model's family accepts
  (`resolve_thinking_attrs`): `thinking_level="low"` on Gemini 3, `thinking_budget=0` before it —
  pairing the wrong one raises inside the plugin on the first live turn. Latency wins:
  preemptive_generation (fed by Flux eager EOT) + minimal thinking. Keep `EnglishModel` turn
  detection — dropping it falls back to dumb VAD-silence detection.
- **`livekit.plugins.turn_detector` is deprecated** (1.6.x) in favour of
  `livekit.agents.inference.TurnDetector`. Unlike the rest of `inference.*`, that one has a
  local path and so is NOT automatically off-limits: `version="v1"` calls the LiveKit
  inference gateway (needs `LIVEKIT_INFERENCE_URL` + api key/secret — off-box, forbidden),
  while `version="v1-mini"` runs in-process through the native `livekit-local-inference`
  wheel (`inference/eot/transports.py::_LocalTransport`). Left unset, the version resolves to
  `v1` only when hosted on Cloud or in `dev`/`console` mode, else `v1-mini` — so a
  self-hosted `start` worker defaults to the local model. Deprecated, not removed, so no
  rush; when it is time to migrate, pin `version="v1-mini"` EXPLICITLY rather than relying on
  that default, and get the boundary call confirmed in review — do not read this note as
  clearance.
- **`livekit-agents` must stay `>=1.6.7`.** Two floors sit on this pin. The *security* one:
  1.6.4/1.6.5 pin `json-repair==0.59.10` (GHSA-xf7x-x43h-rpqh) and the root
  `override-dependencies` that used to force the patched version is gone, so 1.6.6 is the
  lowest safe release — asserted in `tests/unit/test_dependency_floors.py`. The *correctness*
  one, which is why the floor was 1.6.4 to begin with: below that, an agent handoff desynchronizes the STT
  stream's clock from the audio anchor, so a turn where VAD saw no speech energy gets its
  end-of-turn wait set from a future-dated timestamp — unbounded, never clamped by `max_delay`.
  The reply is generated and parked before TTS, and the caller hears dead air until they speak
  again (dev trace `863ba65ac918521c0518aeceea1d3d0b`: 67s of it in one call). Full mechanism
  and the regression tests are in `apps/agent_worker/tests/unit/test_turn_commit.py`; the
  harness beside it (`cascade_harness.py`) is the only way to exercise the turn-commit state
  machine, since the evals have no STT and `session.run()` bypasses `AudioRecognition`.
- **All turn handling goes in the single `turn_handling` block** (endpointing,
  preemptive_generation, turn_detection, interruption). It's mutually exclusive with the
  deprecated flat `AgentSession` kwargs — split them and the omitted pieces are silently dropped.

## No PHI seams (tokenization removed)

The stt/tts PHI redact/hydrate seams (`vera_core.phi` + `seams.py`) were **removed on
2026-07-13**: agents are plain LiveKit agents and the transcript reaches the LLM as raw
values. Every pipeline hop is inside the BAA-covered boundary (repo-root `CLAUDE.md`), so
there is no in-pipeline de-identification — do not re-add `stt_node`/`tts_node` redact/hydrate
overrides without a compliance decision. PHI discipline that remains: never log/trace raw
transcript text, and never put PHI in a room name (spans carry tenant/call UUIDs only).

## Settings & secrets

- `settings.livekit_url` (`VERA_LIVEKIT_URL`): `ws://` local, `wss://` prod; unset →
  `build_livekit_gateway` raises.
- Secrets `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` via the `SecretProvider`, never plaintext
  config. Dev docker-compose uses `--dev` defaults (`devkey`/`secret` at `ws://localhost:7880`).
- Worker also needs `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, and Vertex/Gemini creds
  (`adr/devops-todo.md` #3–6).
