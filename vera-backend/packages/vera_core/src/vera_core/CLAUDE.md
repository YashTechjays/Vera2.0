# vera_core — crypto, RLS, audit, observability (scoped)

Inherits the repo root `vera-backend/CLAUDE.md`. This is the security-critical core;
changes here carry the highest blast radius in the repo.

## No PHI tokenization (the voice pipeline)

The `vera_core.phi` tokenization wall (`PHIBoundary` over `phi_codec`; the `redact` /
`hydrate_for_speech` / `hydrate_raw` crossings and the session vault) was **removed on
2026-07-13**, along with the vendored `phi_codec` package. PHI now flows in **plaintext**
through the live pipeline — every hop (Deepgram, Vertex Gemini, Cartesia, Twilio, LiveKit)
is inside the BAA-covered trust boundary (repo-root `CLAUDE.md`). Do not reintroduce a
codec/boundary here without a compliance decision. De-identification survives only for what
LEAVES the boundary — logs, traces, spans (see Observability below).

**PHI at rest:** structured PHI in Postgres/Redis relies on Google **CMEK** at the storage
layer (Cloud SQL + Memorystore). Application-level column envelope encryption (`protect` /
`reveal`, `*_ct` columns) is **deferred to a later decision** — do not introduce it here
until that decision is made.

## Out-of-pipeline LLM calls go through `vera_core.llm.ResilientLLM` — always

Any LLM call outside the live voice cascade (summaries, analytics, extraction,
post-call processing) MUST be made through `vera_core.llm.ResilientLLM` with
`LLMSpec` provider/model selectors — never by instantiating a provider SDK or a
LiveKit plugin LLM client directly at a call site. ResilientLLM wraps
livekit-agents' FallbackAdapter (ordered provider chain, per-attempt timeout,
retries) and is the single place provider construction, secret resolution
(`OPENAI_API_KEY` via SecretProvider), and PHI-safe error logging live. Adding a
provider means one entry in `vera_core.llm.PROVIDERS`, nothing else. The live
cascade's LLM (the agent worker's AgentSession) is separate and stays in
`apps/agent_worker` — do not route it through ResilientLLM.

## Envelope encryption (`vera_core.config.kms`)

TOTP seeds are envelope-encrypted: AES-256-GCM under a per-user DEK, DEK wrapped by a
`KeyManagementService`. The DEK is ephemeral — never persisted. Use `seal(kms, plaintext)` /
`open_sealed(kms, ...)` from `vera_core.config.kms` for any new credential that needs the
same pattern. Never store the plaintext DEK or the plaintext seed anywhere outside the call stack.

In dev, `LocalDevKMS` wraps DEKs with AES-256-GCM under `LOCAL_KMS_MASTER_KEY` (env var).
In prod, `GCPCloudKMS` delegates to Cloud KMS (Workload Identity, see `adr/devops-todo.md` #2).
`build_kms(settings)` selects the implementation: set `VERA_KMS_KEY_NAME` → GCP; unset → local.

## RLS is authorization, not encryption

`vera_core.db.rls` pins every request transaction to `app.tenant_id` via `SET LOCAL`
(fail-closed: unset GUC → zero rows, `FORCE` RLS). RLS stops cross-tenant **row** access; it
does not protect a row's **contents**. Both are required; neither substitutes for the other.
Always do PHI work inside a `tenant_session(...)`.

## Audit — the HIPAA evidence trail (`vera_core.audit`)

Every PHI access and every authz allow/deny writes an `AuditRecord`:
`{actor, tenant, resource, fields, action, ts}`. Record field **names**, never field
**values**; carry tokens / counts / entity types only. The `audit_log` is append-only (the
migration makes it INSERT/SELECT-only, FORCE RLS) — never `UPDATE` or `DELETE` it, and never
add a code path that mutates a past record.

**Never construct `AuditRecord(...)` or `AuthAuditRecord(...)` directly at a call site —
go through the shared emit helper.** `AuthAuditRecord` → `emit_auth_event()` (`vera_core.audit`,
no `control_plane` dependency by design, so `auth/rbac.py` can call it without circularly
importing `api/v1/common`); a PHI-read `AuditRecord` → `emit_phi_read_audit()`
(`control_plane/api/v1/common.py`). Both exist because hand-rolled construction at each
call site silently drifted on shape — several endpoints forgot `request_id` or
`elevation_session_id` (the field linking a PHI read back to an active superadmin elevation
grant) before these helpers existed. A new event shape that doesn't fit either helper is a
signal to extend it or add a sibling — not license to hand-roll the record again.

**Timestamps come from the DB clock, never the app clock.** Every `created_at`/`updated_at`
and every audit/elevation time is minted by Postgres `now()` / `func.now()` (the `db/base.py`
mixins; the SECURITY DEFINER fns) — never Python `datetime.now()`. A single NTP-synced clock
of record is a HIPAA audit-integrity requirement; app-clock writes add cross-node skew. Even
future-bounds are DB-computed (`tenant_elevation.expires_at = now() + interval`).

## Observability (`vera_core.observability`, Langfuse)

**Any span wrapping a provider call opens through `observability.phi_safe_span`** — never a
bare `start_as_current_span`. OTel defaults `record_exception` / `set_status_on_exception` to
ON, and a provider error can embed the request payload (a transcript, a supervisor's audio, an
extracted answer): the first copies that message into a span event, the second into the status
description, and both then leave the boundary on export. `call_scoped_span` is the same thing
plus the call's trace parent, for spans that belong to a call. Pre-existing hand-rolled sites
in `observer.py`, `health_observer.py` and `queue_dispatcher.py` still pass the two keywords by
hand — migrate them when you touch them.

Structured logs and Langfuse spans are **outside** the PHI-plaintext set — scrub before emit.
Never `logger.*(f"...{plaintext}...")`. Trace token IDs, reference IDs, counts, and shapes —
never raw values. Any trace or log of transcript content must be scrubbed to IDs / counts /
shapes first — there is no in-pipeline tokenizer to lean on now (`vera_core.phi` is gone), so
treat raw transcript text as unloggable. Langfuse may carry reference IDs and timings
(operational shape), never raw PHI.

### Cost tracking — the contracts you can break silently

Vera emits raw **usage** and holds **no prices**; Langfuse prices it against entries seeded
by `scripts/seed_langfuse_prices.py`. Every failure mode below renders a plausible number or
a blank `$` rather than an error, and **no test fails**, so treat these as hard rules.

- **Usage-key strings are a cross-system contract.** `stt_audio_ms`, `tts_characters`,
  `input`, `output`, `cached` live once in `observability/usage_spans.py` and are imported
  everywhere — the emitters (`usage_spans.py`, `llm_usage_export.py`, `control_plane/llm.py`)
  and the seeder. Renaming one without the other zeroes that cost with a green suite, because
  the tests assert against the same constant. If you rename, change the seeded entry too.
- **Only `generation` (and `embedding`) observations carry cost.** A plain span ingests
  cleanly and prices at nothing. Set `langfuse.observation.type` explicitly; never rely on
  Langfuse's implicit "span with a model attribute" promotion.
- **Usage values must be integers.** Langfuse stores them as `Map(String, UInt64)`: a float is
  truncated on the OTel route and dropped entirely on the SDK route. Audio is reported in
  whole **milliseconds**, rounded in Vera — never seconds, and never left to ingestion.
- **`input` excludes cached tokens.** Providers report a prompt count that *includes* them, so
  `input + cached` must always reconstruct that original count. Over-count and cache hits are
  billed twice; omit `cached` and they vanish. `just langfuse-verify` checks this invariant.
- **Thinking tokens bill as output.** The Google plugin's `completion_tokens` excludes them
  while `total_tokens` includes them, so `llm_usage_export` derives them as the residual. Any
  new LLM surface must count them too, or its output is understated. The residual is refused
  outright when the prompt count is absent — otherwise the WHOLE prompt would price as output,
  ~8x the input rate — and whatever it does derive is published as `vera.llm.thinking_tokens`
  so it stays auditable: nothing reconciles output the way `just langfuse-verify` reconciles
  `input + cached`.
- **Never price a span twice.** The SDK's own `llm_request` span is corrected *in place* by
  the export wrapper. A sibling Vera-owned generation for the same request would be summed by
  Langfuse and double-count it.
- Cross-process spans join the call's trace via `TraceLinkStore` (a traceparent in Redis under
  the room name). Langfuse's per-**session** cost rollup is unreliable for model-calculated
  cost, so per-**trace** is the unit that must hold. Open them with
  `observability.call_scoped_span`.
- **Never wrap a redis-py call in `asyncio.timeout`.** Reads are already bounded —
  redis-py defaults `socket_timeout` to 5s and disconnects the connection itself before
  raising. Cancelling mid-command instead leaves the reply unread in the socket while
  `execute_command` returns the connection to the pool (`except Exception` misses
  `CancelledError`), so the next caller reads the previous command's reply. Tighten it
  with `create_redis(socket_timeout=...)`, never a cancellation.
- **A model Vera routes to but deliberately does not price goes in the seeder's
  `KNOWN_UNPRICED`**, with the reason. `just langfuse-verify` reports those without failing;
  anything else unpriced fails it. Never silence the gate by widening a match pattern.

Adding a model Vera can route to means adding its price entry (`GEMINI_MODELS` or `MODELS`)
— otherwise its spend renders blank. The seeder and `just langfuse-verify` both warn by name.
