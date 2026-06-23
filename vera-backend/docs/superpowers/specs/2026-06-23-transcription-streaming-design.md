# Live transcription streaming — Redis stream → authenticated SSE

**Date:** 2026-06-23
**Status:** Approved design, ready for implementation
**Builds on:** the Voice Lab feature (`spec/voice-lab-session`,
`2026-06-23-voice-lab-session-design.md`) — reuses its room/session model, the
`DELETE /voice-lab/sessions/{room_name}` endpoint, and the dispatch-metadata seam.

## 1. Purpose

Stream the live call transcription out of the agent worker so it can be consumed in
real time. The worker publishes each finalized turn to a **Redis stream** keyed by the
room name; the control plane exposes an **authenticated, RBAC-gated SSE endpoint** that
replays the stream from the start and then tails it live; the Voice Lab UI renders the
ongoing transcript. When the session ends, the stream is cleared from Redis.

Decoupling the worker (producer) from consumers via a Redis stream means anything with
the room name and the right permission can consume the transcript from anywhere — the
browser today, future analytics/QA tooling later — without coupling to the worker.

## 2. Scope

### In scope
- Worker: publish finalized **user + agent** turns to a Redis stream (one entry per
  turn), gated by a dispatch-metadata flag so the existing `/calls` path is unchanged.
- A shared `vera_core` transcript module: the stream-key helper, the event model, and
  the Redis store (publisher + reader) behind a Protocol, with an in-memory impl for tests.
- Control plane: `GET /api/v1/voice-lab/sessions/{room_name}/transcript` — an SSE
  endpoint, fully authenticated + `require("calls:read")` + tenant-scoped + audited,
  that replays then tails the stream.
- Frontend: a `streamTranscription` client (fetch + ReadableStream) and a transcript
  panel in the Voice Lab page, opened on session start and torn down on End session.
- Stream lifecycle/cleanup: graceful end sentinel + short grace TTL + a rolling backstop
  TTL, owned by the worker.

### Out of scope / non-goals
- **No persistence.** The transcript lives only in the ephemeral Redis stream; no DB
  rows, no transcript history after the stream clears.
- **No raw-PHI transcript.** The stream carries only the **tokenized / de-identified**
  text that crosses to the LLM (see §7). It never carries hydrated raw PHI.
- **No interim/partial results.** Only finalized turns (one event per completed
  utterance). Interim STT results are not streamed.
- **No `/calls` consumption surface.** Publishing generalizes to `/calls` by setting the
  metadata flag, but the SSE endpoint and UI are Voice-Lab-scoped for now.
- **No speaker diarization beyond role** (`user` vs `agent`).

## 3. Starting point — what already exists

Confirmed present; reuse directly:
- `vera_core.redis.create_redis(redis_url) -> redis.asyncio.Redis` and
  `settings.redis_url` (`VERA_REDIS_URL`). The control plane already builds one client in
  its lifespan (`app.state.redis`); the **worker is not yet connected to Redis**.
- The repo's **Protocol + Redis-impl + in-memory-impl** pattern (session store,
  permission cache, idempotency).
- Worker PHI seam: `stt_node` redacts FINAL/PREFLIGHT user transcripts **before** the
  LLM; `transcription_node` is the reserved tap for tokenized assistant text; the
  `AgentSession` emits `user_input_transcribed` and `conversation_item_added`. The PHI
  boundary is `PassthroughPHIBoundary` (no-op) today — so the "tokenized" text is
  currently identical to what the LLM already receives.
- `vera_core.observability.correlation`: `room_name_for_call`, `parse_room_name`
  (→ `RoomRef(tenant_id, call_id) | None`) — the tenant-scope guard.
- `api/v1/voice_lab.py`: `start_voice_session` (mints the room + dispatch metadata) and
  `end_voice_session` (`DELETE`, tenant-scoped via `parse_room_name`).
- `LiveKitGateway.create_call_room(room_name, metadata=...)` — the dispatch-metadata seam
  already used for `wait_for_speaker`.
- Control-plane display-path chain: `current_identity → tenant_context → require(...) →
  tenant_scoped_session → audit`; the `AuditSink` (`get_audit` dep); the `ResponseModel`
  envelope and `CustomAPIException` error contract.
- Frontend: `lib/api/voiceLab.ts`, `pages/VoiceLab.tsx`, `lib/auth/storage.ts`
  (`getToken()`), the `<LiveKitRoom>` session panel.

### Must be ADDED
- `redis` dependency in `apps/agent_worker/pyproject.toml` (the worker has none today).

## 4. Architecture

```
┌─ agent_worker ──────────────┐                 ┌─ control_plane ─────────────────────┐
│ AgentSession events         │                 │ GET /voice-lab/sessions/            │
│  user_input_transcribed ───▶│   XADD          │     {room}/transcript  (SSE)        │
│  conversation_item_added ──▶│  ────────────▶  │  1 require("calls:read") + tenant   │
│ TranscriptPublisher         │  vera:transcript│  2 audit transcript:read            │
│  (post-redaction text only) │  :<room_name>   │  3 release DB session               │
│ on shutdown: ended + EXPIRE │   (Redis stream)│  4 XRANGE 0..$ replay → XREAD tail  │
└─────────────────────────────┘                 │     ──── text/event-stream ───▶     │ browser
                                                 └─────────────────────────────────────┘
```

Five units, each independently testable.

### 4.1 Shared transcript module — `vera_core/transcript.py` (new)

The single source of truth shared by both processes (room-name-is-the-key principle):

- `transcript_stream_key(room_name: str) -> str` → `f"vera:transcript:{room_name}"`
  (mirrors the existing `vera:sess:` / `vera:perms:` / `vera:idem:` keyspace).
- `TranscriptEvent` (pydantic `BaseModel`): `role: Literal["user", "agent"]`,
  `text: str`, `ts: int` (epoch ms). The terminal end marker is a separate sentinel,
  not a `TranscriptEvent`.
- Constants: `ROLE_USER`, `ROLE_AGENT`, and the `ENDED` sentinel marker (a stream entry
  with field `event=ended`).
- `TranscriptStore` (Protocol) — the low-level transport — with a
  `RedisTranscriptStore(redis)` impl and an `InMemoryTranscriptStore` impl for tests:
  - **publisher side**: `publish(room_name, event)` → `XADD key * role <r> text <t>
    ts <ts>` then refresh the backstop TTL; `mark_ended(room_name)` → `XADD` the `ended`
    sentinel then `EXPIRE key <grace>`; `delete(room_name)`.
  - **reader side**: `read(room_name) -> AsyncIterator[(entry_id, TranscriptEvent)]` —
    replay (`XREAD` from `0`) then tail (`XREAD BLOCK`), stopping on the `ended` sentinel
    or when the key disappears. Each item carries its Redis entry id for the SSE `id:`.
- `TranscriptService(store)` — **the reusable domain API** every consumer/producer goes
  through (so no caller touches raw Redis): `publish_turn(room_name, role, text, *, ts)`,
  `consume(room_name)` (the shared replay-then-tail iterator — SSE frames over it, the
  future finalizer drains it), `collect(room_name)` (drain an ended stream to a list, for
  the finalizer/tests), `end(room_name)`, `clear(room_name)`. The worker publishes via
  the service; the control plane consumes via the service; both are injected a
  `TranscriptService` rather than a bare store.

Keeping the schema, key, transport, and service in one module means the worker and the
control plane cannot drift, and the **consume method is defined once and reused**
everywhere transcripts are read.

### 4.2 Worker — publish transcript (`apps/agent_worker/.../main.py`)

- Add `redis` to `apps/agent_worker/pyproject.toml`; in `entrypoint` build a
  `TranscriptService(RedisTranscriptStore(create_redis(settings.redis_url), ...))` (lazy
  connect, like `LiveKitAPI`); `await redis.aclose()` in the shutdown callback.
- Gate on dispatch metadata: Voice Lab's `create_call_room` passes
  `{"wait_for_speaker": true, "publish_transcript": true}`. If
  `meta.get("publish_transcript")` is falsy → no publishing (so `/calls` is unchanged).
- Register two `AgentSession` handlers that publish via the service
  (`service.publish_turn(...)`):
  - `session.on("user_input_transcribed")` → only when `ev.is_final` → role `user`,
    `text = ev.transcript`. This text is **post-`stt_node`**, i.e. already redacted.
  - `session.on("conversation_item_added")` → only `item.role == "assistant"` → role
    `agent`, `text = item.text_content`. This is the LLM's own token-only output
    (hydration to raw happens later, in `tts_node`, on the audio path only).
  - The implementation verifies, against the installed `livekit-agents`, that these
    events carry the de-identified text (user = redacted FINAL, agent = pre-hydration)
    before publishing; behavior is the contract.
- Publishing is **best-effort**: wrapped in try/except, failures logged, **never break
  the call** (a Redis outage must not fail the voice session).
- On shutdown (the existing `add_shutdown_callback`): `mark_ended(room_name)` (sentinel +
  short grace TTL) so connected readers drain and the stream then self-clears. The worker
  **owns** the stream lifecycle; the control plane does not delete it (avoids an
  `XADD`-after-`DEL` recreate race — see §6).

### 4.3 Control plane — SSE endpoint (`api/v1/voice_lab.py`)

`GET /api/v1/voice-lab/sessions/{room_name}/transcript` → `StreamingResponse`
(`media_type="text/event-stream"`, headers `Cache-Control: no-store`,
`X-Accel-Buffering: no`). Auth/authz/audit run **before** streaming begins:

1. **Authenticate + authorize** — `require("calls:read")` (same gate as Voice Lab start).
2. **Tenant scope** — `ref = parse_room_name(room_name)`; if `ref is None` or
   `ref.tenant_id != tenant_id` → `404` (cross-tenant guard, identical to
   `end_voice_session`).
3. **Audit** — write a transcript-access audit record (actor `user_id`, tenant,
   resource `room_name`, action `transcript:read`; **field names only**, no values).
4. **Release the DB session, then stream.** An SSE response is long-lived; FastAPI's
   `require()` chain opens a request-scoped RLS DB session, and holding a DB connection
   for the whole stream would starve the pool. So the endpoint resolves
   authz + writes the audit row via a **short-lived** session (opened and committed in
   the handler body, not a response-spanning `yield`-dependency), and the streaming
   generator then touches **Redis only**. The exact mechanism (explicit `async with
   sessionmaker()` for the authz/audit step vs. a scoped dependency) is settled at
   implementation; the contract is: no DB connection is held for the stream's lifetime.
5. **Stream** — iterate `service.consume(room_name)`, emitting each item as an SSE frame:
   ```
   id: <redis-entry-id>
   data: {"role":"user","text":"...","ts":1750000000000}

   ```
   Close the response when the `ended` sentinel arrives, the key disappears, or the
   client disconnects (the generator observes `await request.is_disconnected()` /
   the cancelled task and closes its Redis reader).

The `TranscriptService` is injected like the other infra deps (a `get_transcript_service`
reading `app.state.transcript_service`, built over the existing `app.state.redis`).

### 4.4 Settings — `vera_core/config/settings.py`

```
# Live-transcript Redis stream lifetime knobs (Voice Lab / SSE).
transcript_stream_ttl_seconds: int = 3600  # rolling backstop: abandoned stream self-clears
transcript_end_grace_seconds: int = 60     # after the ended sentinel, readers drain then clears
```
Document both in `vera-backend/env.example` (`VERA_TRANSCRIPT_STREAM_TTL_SECONDS`,
`VERA_TRANSCRIPT_END_GRACE_SECONDS`).

### 4.5 Frontend — client + transcript panel

- `vera-frontend/src/lib/api/transcription.ts`:
  ```
  streamTranscription(roomName, { signal, onEvent, onError }) : Promise<void>
  ```
  Uses `fetch(`${BASE_URL}/voice-lab/sessions/${encodeURIComponent(roomName)}/transcript`,
  { headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
  signal })`, reads `res.body.getReader()`, parses SSE frames (split on `\n\n`, take the
  `data:` line), and calls `onEvent(TranscriptEvent)`. `signal` (an `AbortController`)
  tears it down. Reconnect = re-call (the endpoint replays from `0`). `EventSource` is
  **not** used — it cannot set the `Authorization` header, and the credential must not go
  in the URL.
- `vera-frontend/src/pages/VoiceLab.tsx`: a `<TranscriptPanel>` rendered alongside
  `<SessionPanel>` inside `<LiveKitRoom>`, keyed by `session.room_name`. It opens the
  stream on mount (an `AbortController` in a `useEffect`), renders an ordered, chat-style
  list of `{role, text}` turns (auto-scroll), and aborts on unmount / `endSession()`.
  Errors surfaced inline (e.g. a `403`/`404` from the endpoint).

## 5. Data flow

```
worker session starts (publish_transcript=true)
  → user/agent turn finalizes → TranscriptEvent → XADD vera:transcript:<room> (+refresh TTL)
browser (Voice Lab, session active)
  → fetch GET .../{room}/transcript  (Authorization: Bearer <session token>)
  → BE: require("calls:read") + tenant-match(room) + audit + release DB session
  → store.read(room): XRANGE 0..+ (full replay) then XREAD BLOCK (tail)
  → SSE frames → panel renders turns in order, live
End session
  → DELETE room (existing) → worker disconnects → shutdown: mark_ended (sentinel + grace TTL)
  → reader sees `ended` → SSE closes; stream self-clears after the grace window
```

## 6. Lifecycle & cleanup (worker-owned, three layers)

1. **During the call** — every `publish` refreshes a **rolling backstop TTL**
   (`transcript_stream_ttl_seconds`), so an abandoned stream (worker crash, browser
   closed without End) self-clears within the TTL.
2. **Graceful end** — on worker shutdown (triggered when End session deletes the room),
   `mark_ended` appends the `ended` sentinel and sets a **short grace TTL**
   (`transcript_end_grace_seconds`) so any connected reader drains the sentinel and then
   the key clears.
3. **Single owner** — only the worker writes/expires the stream. The control plane does
   **not** `DEL` it, which avoids the race where a worker `XADD` after a control-plane
   `DEL` recreates an orphan key. "Cleared on session end" is satisfied within the grace
   window (seconds), which is acceptable for an ephemeral, de-identified stream.

**Forward-compatibility with DB persistence (separate sub-project, §10).** The full
transcript is always replayable from the stream (`XRANGE 0 +`, finals only) and the
`ended` sentinel marks completion — so a future **control-plane transcript finalizer**
can drain the whole stream into the `Transcript` table at real-call end *before* it is
cleared. When that lands, the finalizer owns clearing (drain → persist → `DEL`) and the
worker's grace/backstop TTLs become the safety net only. This spec deliberately does not
build that finalizer (it needs a `Call` row, a call-end trigger reaching the control
plane, and its own PHI-at-rest decision — none of which the Voice Lab harness has), but
its stream shape is the finalizer's source of truth.

## 7. PHI handling

The stream carries **only the tokenized / de-identified text** — exactly the text that
crosses the STT→LLM boundary: user turns are the **redacted** FINAL transcript (post
`stt_node`), agent turns are the LLM's **token-only** output (pre `tts_node` hydration).
This keeps the pipeline on-side of the bright line "never store plaintext PHI in Redis."

Under today's `PassthroughPHIBoundary` (no-op) the tokenized text equals the raw text —
the same de-identified-by-design surface the LLM already receives — so this introduces
**no new PHI exposure** beyond what the LLM already processes. When the real `phi_codec`
lands, the stream automatically carries `[[TYPE_N]]` tokens with no code change here.

RBAC, tenant isolation, and the audit trail are **fully enforced** on the SSE endpoint
(§4.3) — the consumer is an authenticated, `calls:read`-bearing, same-tenant user, and
every stream open is audited. Memorystore Redis is inside the BAA boundary (CMEK at rest).

## 8. Error handling

| Condition | Result |
|---|---|
| Unauthenticated / missing bearer | `401` before streaming |
| Authenticated but lacks `calls:read` | `403` (audited deny) |
| `room_name` malformed or foreign tenant | `404` (`parse_room_name` guard) |
| Stream not created yet (worker still starting) | SSE stays open and tails until the first entry |
| Worker → Redis publish failure | best-effort: logged, the call continues unaffected |
| Client disconnects | generator observes disconnect → closes the Redis reader |
| Session ends mid-stream | `ended` sentinel → SSE closes; key clears after the grace TTL |
| Redis unavailable for the reader | `503`/`ApiError`; the panel shows an error state |

## 9. Testing

- **Shared module** (`tests/unit/...`): `transcript_stream_key` shape; `InMemoryTranscriptStore`
  publish→read round-trip (replay then tail), the `ended` sentinel terminates `read`, and
  `read` stops when the key is gone.
- **Worker** (`tests/unit/worker/...`): the event→`TranscriptEvent` mapping publishes
  only finalized turns and only the de-identified text; `publish_transcript` absent/false
  → no publishing; a publish exception does not propagate (call-safe); shutdown calls
  `mark_ended`.
- **Control plane** (`tests/integration/control_plane/test_transcription.py`): valid
  caller → `200` + `text/event-stream`, replay then live tail (in-memory store);
  unauthenticated → `401`; missing permission → `403`; foreign-tenant/malformed room →
  `404`; an audit record is written; the DB session is not held for the stream lifetime.
- **Frontend** (`vera-frontend/src/lib/api/transcription.test.ts`): SSE-frame parser
  yields ordered `TranscriptEvent`s; `AbortController` tears the stream down; error
  propagation.

## 10. Open follow-ups (not this task)

- **Transcript DB persistence — its own spec (decomposed, the agreed next sub-project).**
  At **real-call** end, a **control-plane** "transcript finalizer" drains the Redis
  stream (this spec's output) into the existing `Transcript` table (`vera_core/models/
  transcript.py`: `call_id` FK, `seq` `UNIQUE(call_id, seq)`, `role`, `source`, `message`
  marked **PHI**, `spoke_at`), then clears the stream. That spec must resolve: how
  call-end reaches the control plane (the worker has no DB and must not get one); whether
  `message` is stored tokenized or re-hydrated to raw at rest (and the session-vault
  timing if raw); and that it applies only to calls with a `Call` row (not the Voice Lab
  harness). The lifecycle here is already forward-compatible with that finalizer (§6).
- A dedicated `transcription:read` permission (vs. reusing `calls:read`) if transcript
  access should be grantable independently of call visibility.
- Generalize publishing to the real `/calls` flow by setting `publish_transcript` there,
  once that flow lands.
- Interim/partial STT results for a finer-grained live view, if desired.
