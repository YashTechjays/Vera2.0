# STT / TTS Usage & Cost Observability in Langfuse — Design

**Date:** 2026-07-28
**Branch:** to be cut off `main` at implementation time (not yet started)
**Status:** Approved design, pending implementation plan
**Builds on:** `2026-07-27-voice-pipeline-observability-design.md` (the `vera.*` attribute
convention and the PHI guardrail below are inherited from it)

## 1. Overview

Langfuse traces for a Vera call show real token counts and dollar cost on the LLM spans
(`llm_request`, e.g. `1,255 -> 17 (sum 1,272) $0.000419`) but **no cost at all** for speech-to-text
(Deepgram) or text-to-speech (Cartesia) — even though both are billed per call and both
already compute their usage inside the process. Two different root causes, one shared fix.

### Goals

- Make per-turn STT and TTS **usage** (audio seconds, synthesized characters) visible in
  Langfuse, on spans that sit inside the call's own trace.
- Make Langfuse compute and render **cost** for that usage, using the same model-price
  mechanism it already uses for the LLM.
- Cover **every** billed STT/TTS surface in Vera, not just the main cascade: the
  supervisor-takeover per-track STT and coaching's hold-to-whisper STT are real spend that is
  invisible today.
- Keep Vera itself free of any pricing knowledge (see §4 D1).

### Non-goals

- **Redacting the pre-existing LiveKit SDK spans.** `llm_node` / `llm_request` / `agent_turn` /
  `user_turn` already attach raw transcript and chat-context text to Langfuse. That is a real
  gap (tracked as `otel-spans-unredacted-pre-prod`), explicitly deferred to its own PHI-redacting
  `SpanProcessor` work, and **not widened here** — see §7.
- **Persisting cost to Vera's own database.** "What did this call cost" as a product-facing,
  queryable field on the `Call` row is a plausible want but a different feature. Langfuse's
  trace-total rollup is the deliverable here.
- **Forcing the coaching-whisper span into the agent worker's trace.** It is a different
  process with no propagated trace context; see §3.4 for why session-level correlation is the
  answer instead.
- **Pricing VAD / EOU / interruption metrics.** Silero VAD and the turn detector run locally
  and cost nothing.
- **Wiring the price seeder into the deploy pipeline.** Tracked as a devops-todo instead (§6).

## 2. Current-state root causes (confirmed by code read + live trace, 2026-07-28)

Verified against `livekit-agents 1.5.17` in `vera-backend/.venv` and against live trace
`c0f327d3fe28b1b5439037dc1e6550db` in the local Langfuse instance.

### 2.1 STT — usage exists in-process and reaches nothing

- `stt/stt.py` contains **zero** tracing code: no span, no attribute, no tracer import. STT
  usage leaves the module only via `self.emit("metrics_collected", stt_metrics)` — three sites:
  `stt.py:212` (one-shot `recognize`), `stt.py:372` (`_report_connection_acquired`, zero-usage),
  `stt.py:449` (`_metrics_monitor_task`, the streaming path Vera actually uses).
- The streaming path fires only on a `RECOGNITION_USAGE` speech event. Deepgram **does** emit it
  with a real `audio_duration` (`plugins/deepgram/stt_v2.py:505-508` for Flux,
  `plugins/deepgram/stt.py:687-690` for v1), so the data genuinely exists.
- `telemetry/otel_metrics.py` forwards it to the **OTel Metrics API** — `_meter =
  metrics_api.get_meter("livekit-agents")`, counter `lk.agents.usage.stt_audio_duration`. That is
  a separate OTel signal from Traces.
- `vera_core/observability/otel.py` configures **only** a `TracerProvider` (grep: no
  `MeterProvider`, no `set_meter_provider`). LiveKit therefore gets OTel's default **no-op**
  meter and the computed metrics are discarded in-process.
- **Wiring a `MeterProvider` would not help.** Langfuse's OTLP endpoint ingests **traces only**;
  there is no metrics-signal ingestion. This is why the fix is span-based for STT too.
- There is no `ATTR_STT_METRICS` in `telemetry/trace_types.py` (there is `ATTR_LLM_METRICS` and
  `ATTR_TTS_METRICS`) — the omission is structural, not incidental.

**Correction to an earlier diagnosis:** it is *not* true that "no STT span exists."
`voice/audio_recognition.py:1383-1391` sets `gen_ai.request.model` and `gen_ai.provider.name`
for STT on the **`user_turn`** span. What is missing is *usage*, not the span.

### 2.2 TTS — a span with usage on it that Langfuse's cost engine cannot read

- TTS **does** get spans: `@tracer.start_as_current_span("tts_node")`
  (`voice/generation.py:255-262`, which also sets `gen_ai.request.model` /
  `gen_ai.provider.name`) and a `tts_request` span carrying
  `lk.tts_metrics = metrics.model_dump_json()` (`tts/tts.py:262` non-streaming, `tts/tts.py:607`
  streaming — Vera's Cartesia websocket path).
- The live trace confirms real usage sitting there unpriced:
  `lk.tts_metrics: {characters_count: 465, duration: 4.72, audio_duration: 27.64, ttfb: 0.134,
  input_tokens: 0, output_tokens: 0, ...}`.
- `lk.tts_metrics` is an arbitrary custom attribute bag, **not** an attribute Langfuse's OTLP
  ingestion parses for usage. `telemetry/trace_types.py:73-86` defines the standardized
  `gen_ai.usage.*` names — with a comment citing
  `https://langfuse.com/integrations/native/opentelemetry#usage` — but grep confirms they are set
  **only** on LLM/realtime spans (`llm/llm.py:342-345`, `voice/run_result.py:979-980`,
  `telemetry/utils.py:37-38`), never on TTS or STT. Hence LLM prices and TTS does not, side by
  side in the same trace.

### 2.3 What Langfuse actually accepts (the enabling finding)

Confirmed from Langfuse's docs and its ingestion source (self-hosted `langfuse/langfuse:3`):

- `langfuse.observation.usage_details` — a JSON-string span attribute with **arbitrary keys**,
  validated as `z.record(z.string(), z.number().nonnegative())`, so **floats are accepted**.
  Keys are matched *exactly* against a model definition's per-usage-type prices and the
  resulting costs summed. Non-token billing units are therefore fully supported.
- `langfuse.observation.cost_details` — used **verbatim**, bypassing Langfuse's own calculation.
- Cost is computed for **any** observation type; a plain SPAN does not need to be a GENERATION.
- Caveat, not applicable to Vera: `isAiSdkAgentSpan` spans deliberately skip usage/cost
  extraction to avoid double-counting.

### 2.4 One uniform hook for both

`STTMetrics` and `TTSMetrics` both flow through the same `metrics_collected` event, funnelled by
`voice/agent_activity.py:1478-1496`. Both dataclasses carry exactly the fields billing needs:

| | field | note |
|---|---|---|
| `STTMetrics` | `audio_duration: float` | seconds of audio processed |
| `TTSMetrics` | `characters_count: int` | `len(self._input_text)` — a length, never the text |
| both | `input_tokens` / `output_tokens: int` | 0 for Deepgram + Cartesia; non-zero only for token-billed providers |
| both | `metadata.model_name` / `.model_provider` | populated by all three plugins Vera uses |

Vera has **no** `metrics_collected` listener today, so this is new wiring rather than a change
to existing behavior.

## 3. Components

### 3.1 New module — `vera_core/observability/usage_spans.py`

Two public functions, deliberately split so the part Langfuse contracts on is testable without
booting anything:

```python
def usage_span_attributes(
    metrics: STTMetrics | TTSMetrics,
) -> dict[str, str | int | float | bool] | None:
    """The exact span attributes for one metrics event, or None when there is no
    billable usage (see §5.1). Pure — no OTel, no I/O."""

def attach_usage_spans(
    emitter: rtc.EventEmitter,
    *,
    parent_context: Context | None = None,
    room_name: str | None = None,
    source: str | None = None,
) -> None:
    """Register a metrics_collected listener that emits one usage span per billable
    event, parented at `parent_context` (see §3.3)."""
```

`attach_usage_spans` accepts any `rtc.EventEmitter` that emits `metrics_collected`, which covers
all four targets below with no per-site special-casing.

### 3.2 The four attach sites

| Site | Emitter | Model string | Trace placement |
|---|---|---|---|
| `cascade.build_session` (`cascade.py:123`) | `deepgram.STTv2` | `flux-general-en` | under `job_entrypoint` |
| `cascade.build_session` (`cascade.py:132`) | `cartesia.TTS` | `sonic-3.5` | under `job_entrypoint` |
| `main.py:541` `stt_factory`, per subscribed track | `deepgram.STT` | `nova-3` | under `job_entrypoint` via captured context |
| `vera_core/stt.py` `ResilientSTT._adapter()` | the `FallbackAdapter` | `flux-general-en` | own trace, same `langfuse.session.id` |

`build_session` currently constructs `stt=` / `tts=` inline as `AgentSession` kwargs; they must be
bound to locals first so the listeners can be attached. The LLM needs nothing — it already prices
correctly.

**`ResilientSTT` attaches to the `FallbackAdapter`, not to each inner STT.** STT's
`FallbackAdapter` re-emits `metrics_collected` (`stt/fallback_adapter.py:277-278`) forwarding the
inner `STTMetrics` verbatim, so `metadata.model_name` is the true provider model
(`flux-general-en`), not the literal `"FallbackAdapter"`. One listener on `self._chain` covers the
whole chain. Attach inside `_adapter()` at chain-construction time (the chain is built lazily on
first `transcribe()`), and note that `aclose()` discards the chain — a rebuilt chain gets a fresh
listener, so there is no leak and no double-registration.

### 3.3 Trace parenting — why the context is passed in explicitly

`job_entrypoint` is a `start_as_current_span` wrapping Vera's entrypoint function
(`ipc/job_proc_lazy_main.py:316-323`), so it *is* the ambient span inside `main.py` — which is why
`main.py:342`'s `trace.get_current_span().set_attributes(...)` already works. OTel context rides
`contextvars`, and `asyncio.create_task` copies the current context.

The takeover STT is reached by **two paths with different contexts**:

| Path | Context |
|---|---|
| `start()` -> `_maybe_transcribe` (`takeover_transcript.py:108-111`) — tracks already subscribed, called synchronously from the entrypoint | inherits `job_entrypoint` |
| `room.on("track_subscribed")` -> `_on_track_subscribed` (`takeover_transcript.py:107,114-118`) — tracks subscribed later | inherits whatever task LiveKit's room event dispatch runs in |

So a supervisor who joins **after** takeover begins goes down path 2. Calling
`context.get_current()` at attach time inside `_transcribe_track` would capture that unrelated
context, and the resulting spans would become **new trace roots** — falling out of the call's
trace entirely and never summing into its cost.

**Therefore:** capture the OTel context **once in the job entrypoint** (where `job_entrypoint` is
provably ambient) and let the `stt_factory` closure carry it. A closure over a value is immune to
whichever task later invokes it, so both paths land under `job_entrypoint` uniformly. This is why
`attach_usage_spans` takes `parent_context` explicitly rather than sniffing ambient context. The
SDK solves the same problem for its `amd` span via `context=self._session._root_span_context`, but
that reaches for a private attribute; capturing `context.get_current()` at entrypoint scope is the
public-API equivalent.

The cascade STT/TTS are constructed inside `build_session`, itself called from the entrypoint
(`main.py:432`), so they can capture the same context.

### 3.4 Coaching whisper is a separate trace, by necessity

`job_entrypoint` lives in the **agent worker** process for that call's job. The whisper request is
an HTTP request handled by the **control plane** (`coaching.py:158` `on_demand_transcribe`), called
directly by the supervisor's browser. No trace context crosses between the two processes, and
nothing persists the worker's trace id for the control plane to adopt as a remote parent. Its span
is therefore its own trace, correlated by `langfuse.session.id = room_name` — the same mechanism
the prior design uses for control-plane dispatch spans. `on_demand_transcribe` has `call_id` and
`tenant_id` in scope, so `call_trace_attributes(room_name_for_call(tenant_id, call_id))` gives it
the correct session id.

Unifying the traces would mean stashing the worker's trace/span id in Redis per call and injecting
a remote parent in the control plane — a new cross-process pattern for modest gain. Explicitly not
done.

### 3.5 Dual-channel takeover attribution

A takeover with both the callee's and an intervening supervisor's audio produces **two**
`_transcribe_track` tasks and therefore two STT instances (`takeover_transcript.py:134-138`), both
correctly billed. `stt_factory` is currently `Callable[[], STT[Any]]` — no arguments — so a span
cannot say which channel it billed.

Widen it to `Callable[[SpeakerAttribution], STT[Any]]` and pass
`attribution.source` through as `vera.usage.source`. `SpeakerAttribution.source` is a
`TurnSource` closed enum (`SOURCE_REP` / `SOURCE_SUPERVISOR`, `takeover_transcript.py:40-53`), so it
is PHI-safe per §7. `attribution.user_id` is **not** attached — it adds nothing to cost.

This is the only change to `takeover_transcript.py`.

## 4. Decisions (settled during brainstorming)

| # | Decision | Choice |
|---|---|---|
| D1 | Who owns the per-unit prices | **Langfuse model config.** Vera sends raw `usage_details` only and holds no rate anywhere. A price change never needs a Vera deploy; the cost is that each Langfuse environment must be seeded (§6). |
| D2 | Where usage attributes land | **One Vera-owned span per metrics event** (`vera.stt.usage` / `vera.tts.usage`). Per-turn attribution; Langfuse still sums them into the trace total. |
| D3 | Which hook | **Component-level `emitter.on("metrics_collected", ...)`**. `session.on("metrics_collected")` is deprecated in 1.5.17 and logs a warning per registration (`voice/agent_session.py:491-497`); the component emitters are not (`stt/stt.py:145-148`) and are what the SDK itself uses (`agent_activity.py:683,687`). Rejected: `session_usage_updated`, whose `AgentSessionUsage.model_usage` is cumulative per `(provider, model)` and would need stateful diffing, has already discarded per-request identity, and cannot see the takeover STT. |
| D4 | Billing unit | **Float seconds / integer characters** (see §5.3). |
| D5 | Price seeding | **Idempotent script** + `just` recipe, plus a manual runbook and a devops-todo row (§6). |
| D6 | Definition of done | **Live end-to-end verification** against local Langfuse, not unit tests alone (§8.3). |
| D7 | Takeover + coaching STT | **In scope.** Both are billed STT that is invisible today. |
| D8 | Rewriting spans at export time | **Rejected.** Reading `lk.tts_metrics` off the SDK's `tts_request` span in an exporter wrapper would price TTS with no new spans, but no span anywhere carries STT usage — so it fixes half the problem and leaves two mechanisms to maintain. |

## 5. Attributes and data shape

Span names: **`vera.stt.usage`**, **`vera.tts.usage`**.

```
# vera.tts.usage — one TTS request
gen_ai.request.model                = "sonic-3.5"
gen_ai.provider.name                = "Cartesia"
langfuse.observation.usage_details  = '{"tts_characters": 465}'
vera.usage.streamed                 = true
vera.usage.cancelled                = false
vera.usage.audio_seconds            = 27.64      # operational only, NOT priced
<call_trace_attributes(room_name)>               # vera.room, vera.tenant_id,
                                                 # vera.call_id, langfuse.session.id

# vera.stt.usage — one STT recognition-usage event (cascade: no source attribute)
gen_ai.request.model                = "flux-general-en"
gen_ai.provider.name                = "Deepgram"
langfuse.observation.usage_details  = '{"stt_audio_seconds": 27.64}'
vera.usage.streamed                 = true
<call_trace_attributes(room_name)>

# vera.stt.usage — the takeover per-track variant, which alone carries a source
gen_ai.request.model                = "nova-3"
gen_ai.provider.name                = "Deepgram"
langfuse.observation.usage_details  = '{"stt_audio_seconds": 12.10}'
vera.usage.streamed                 = true
vera.usage.source                   = "supervisor"
<call_trace_attributes(room_name)>
```

`gen_ai.request.model` comes from `metrics.metadata.model_name`, which every plugin Vera uses
populates with the literal string Vera passed (`plugins/deepgram/stt_v2.py:173`,
`plugins/deepgram/stt.py:199`, `plugins/cartesia/tts.py:202`) — giving the seeder's `matchPattern`
an exact, predictable target. `gen_ai.provider.name` is the plugin's fixed `"Deepgram"` /
`"Cartesia"`.

`input_tokens` / `output_tokens` are folded into `usage_details` **only when non-zero**. Both
dataclasses carry them for token-billed providers; Deepgram and Cartesia report 0. A zero-valued
key would demand a price entry for a unit nobody bills — and if a token-billed TTS is ever
adopted, the keys appear automatically.

### 5.1 Zero-usage events are dropped

`stt.py:369-383` `_report_connection_acquired` emits a genuine `STTMetrics` with
`audio_duration=0.0` and `request_id=""` purely to report websocket connection timing. Emitting a
span for it would add a `$0` noise span per connect. Rule: return `None` from
`usage_span_attributes` when every billable quantity is zero — STT when
`audio_duration == 0 and input_tokens == 0 and output_tokens == 0`, TTS when
`characters_count == 0 and input_tokens == 0 and output_tokens == 0`.

### 5.2 Cancelled TTS still counts

On barge-in, `TTSMetrics.cancelled=True`. Those characters were already sent to Cartesia and are
still billed, so the span **is** emitted and the characters **are** counted, tagged
`vera.usage.cancelled=true` so barge-in spend is queryable. Suppressing cancelled events would
under-report real money.

### 5.3 Units: float seconds, not integer milliseconds

Langfuse's OTel ingestion validates `usage_details` as
`z.record(z.string(), z.number().nonnegative())`, so floats are accepted. Its **public read**
schema declares `usageDetails` as `map<string, integer>` — the one soft spot: if a future version
tightened ingestion to match, fractional seconds would silently truncate. Milliseconds would be
immune but make the rate an awkward per-millisecond figure and render `27640` in the UI. Seconds
is the better default; §8.3 step 5 is what confirms it survives ingestion.

## 6. Price seeding

### 6.1 `scripts/seed_langfuse_prices.py` + `just langfuse-seed-prices`

Follows the `scripts/bootstrap_platform_admin.py` pattern: idempotent, env-driven, run-on-demand.

- **Auth needs no new config** — basic auth built from the existing `langfuse_public_key` /
  `langfuse_secret_key` / `langfuse_host` in `Settings`, the same values `configure_observability`
  uses. Logs the target host; never prints the secret.
- **Rates come from script-scoped env vars, not `Settings`** — following the
  `SEED_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_EMAIL` precedent (`os.environ`, no `VERA_` prefix). The
  application never needs a price (Langfuse does the arithmetic), so this keeps exactly one place
  prices live, with no drifting second copy inside Vera. Reinforces D1.

Three entries:

| `modelName` | `matchPattern` | prices |
|---|---|---|
| `vera-deepgram-flux` | `(?i)^flux-.*$` | `{"stt_audio_seconds": <rate>}` |
| `vera-deepgram-nova` | `(?i)^nova-.*$` | `{"stt_audio_seconds": <rate>}` |
| `vera-cartesia-sonic` | `(?i)^sonic-.*$` | `{"tts_characters": <rate>}` |

Prices go in via `pricingTiers[0].prices`, **not** the deprecated flat
`inputPrice`/`outputPrice`/`totalPrice`, which cannot express a custom usage key at all.

**Family patterns, not exact versions.** `(?i)^sonic-.*$` rather than `^sonic-3\.5$`, so bumping
`sonic-3.5` -> `sonic-4` does not silently zero the cost — a missing match renders identically to
"no cost data," making that failure mode invisible. Tradeoff: if two versions in a family are
priced differently, the family entry is wrong for one; Langfuse's `startDate` handles that when it
happens.

**Three entries cover today's models, but the model strings are configurable.** The whisper
chain reads `whisper_stt_primary_model` (default `"deepgram:flux-general-en"`, overridable via
`VERA_WHISPER_STT_PRIMARY_MODEL`) and `whisper_stt_fallback_models` (default
`["assemblyai:best"]`, `settings.py:198-199`). AssemblyAI has no API key provisioned, so its
factory is dropped at construction today — but if it is ever provisioned, `best` matches **none**
of the three patterns above and its usage would render `$0` with no other signal. Same risk if
either env override points at an unseeded model family. Two mitigations, both cheap: the seeder
logs the full set of `modelName`/`matchPattern` pairs it wrote so a reader can compare them
against the configured selectors, and §8.3 step 4 checks cost is non-blank on every usage span
rather than only on the cascade's. Adding a fourth entry when AssemblyAI is provisioned is a
one-line change to the seeder's table.

**Idempotency has a specific shape.** `POST /api/public/models` upserts *only* when passed an
existing `modelId`; a duplicate `modelName` without one is rejected on the
`(projectId, modelName)` uniqueness check. So the script GETs the model list, matches by
`modelName`, and threads the existing `modelId` back into the POST. Re-running is a no-op-shaped
update, never a duplicate-key error.

**It refuses to seed a zero.** A missing or unparseable rate env var exits non-zero having written
nothing. A `$0.00` price entry is indistinguishable from broken instrumentation in the UI, so a
partial seed is worse than no seed.

**Real rates are a human input.** Public list prices — roughly `$0.0077`/min for Deepgram
(~`$0.000128`/second) and `$5-37` per million Cartesia characters, plan-dependent — are a sanity
reference only, not the contracted rate. Because the script demands the env vars, no placeholder
price can ship in code.

The script writes to whatever `VERA_LANGFUSE_HOST` resolves to, so running it with production env
targets production. It is config-only, idempotent and non-destructive, so no confirmation prompt —
just the target-host log line.

### 6.2 Manual runbook — `docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md`

The by-hand fallback (no shell access to an environment, adjusting one rate in the UI, a project
stood up by hand). Follows the `2026-07-20-call-health-observer-manual-test-guide.md` precedent of
a guide living beside its design. Contents:

- The click path — Langfuse -> project -> **Settings -> Models -> + New model** — and which UI
  field maps to which API field (the UI says "match pattern" and "price" where the API says
  `matchPattern` and `pricingTiers[0].prices`).
- The three entries as a fill-in table: `modelName`, `matchPattern`, usage key, price.
- **How to discover the usage key from a live span** rather than trusting the doc: open any
  `vera.tts.usage` observation and read the keys off its `usage_details`. Keeps the runbook
  self-correcting if attribute names change.
- A "cost is blank — why" triage table, since all four causes look identical in the UI: no model
  entry · `matchPattern` does not match the ingested `gen_ai.request.model` · price key != usage
  key · usage key typo'd in the instrumentation.
- The two §6.1 warnings restated where a human hits them: a `$0` entry looks like broken
  instrumentation, and an exact-version pattern silently zeroes cost on the next model bump.
- Public list prices as a sanity reference, flagged as *not* the contracted rate.
- An up-front pointer that `just langfuse-seed-prices` is the preferred path and this is the
  fallback — so the doc does not quietly become the primary route and drift from the script.

### 6.3 `adr/devops-todo.md` row 22

| # | Item | Why it matters | Source |
|---|---|---|---|
| 22 | ☐ **Seed the Langfuse custom model price entries in every environment** — `just langfuse-seed-prices` with the three rate env vars set, creating `vera-deepgram-flux`, `vera-deepgram-nova` and `vera-cartesia-sonic` with the real contracted per-second / per-character rates. Re-run after any Langfuse project re-provision (the entries live in Langfuse's own DB, not in this repo) and after any STT/TTS model-family change. Manual fallback: `docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md`. | STT/TTS usage attributes ingest fine without a price entry, but every observation then renders blank cost — so runaway spend and cost regressions stay invisible, and a missing entry is indistinguishable in the UI from broken instrumentation. The rates are contract-specific rather than public list price, so they cannot ship in code; they are not secrets, just values that must exist wherever the seeder runs. | STT/TTS usage & cost observability (2026-07-28); spec `docs/superpowers/specs/2026-07-28-stt-tts-cost-observability-design.md`. |

The seeder is deliberately **not** wired into the deploy path: it is Langfuse-side config that
changes on the order of once per environment, and folding it into every deploy would ship rate env
vars to a place that otherwise never needs them. This row is the tracking mechanism instead.

## 7. PHI guardrail (hard requirement)

The repo rule is unconditional: never log, print, trace, or attach to a span plaintext PHI
(`vera-backend/CLAUDE.md`, enforced by a PreToolUse hook). The pre-existing SDK spans already
violate it (§1 Non-goals) — accepted, tracked, deferred, and **not** a license to add more.

Neither `STTMetrics` nor `TTSMetrics` carries **any** text field. `characters_count` is
`len(self._input_text)` (`tts/tts.py:249`) — a length, never the string. Every attribute in §5 is
one of:

- a count or duration (`characters_count`, `audio_duration`, token counts)
- a boolean (`streamed`, `cancelled`)
- a closed enum (`vera.usage.source` -> `TurnSource`; `gen_ai.provider.name` -> a plugin constant)
- a fixed model name Vera itself passed in (`flux-general-en`, `nova-3`, `sonic-3.5`)
- the existing `call_trace_attributes` set (room name + tenant/call UUIDs), already established
  as span-safe by the prior design

**Never**, anywhere in this implementation: transcript text, `SpeechEvent.alternatives[0].text`
(handled nearby in `publish_final_turns`), extracted answer values, or DTMF digits.

**Two explicit prohibitions beyond the allow-list:**

1. Do **not** copy the SDK's `metrics.model_dump_json()` blob onto Vera spans (the
   `lk.tts_metrics` pattern). It carries no PHI today, but it attaches whatever fields a future
   SDK version adds, sight unseen.
2. Do **not** attach `attribution.user_id` (§3.5) — it contributes nothing to cost.

Any implementation step that would attach a value outside the allow-list comes back to this
design for a decision rather than being added ad hoc.

## 8. Error handling and testing

### 8.1 Error handling

Every attach and every span emit is wrapped so a tracing failure can never affect the call, the
transcript, or the whisper request — the same principle as the cursor write in the prior design:
`try/except Exception: logger.warning(..., type(exc).__name__)`. Never a bare `except` that would
swallow `asyncio.CancelledError`. Log the exception **type name only**, never its repr or
traceback, per `phi-safe-exception-logging` discipline. The listeners are invoked synchronously
from `rtc.EventEmitter.emit`, so the handler stays sync and cheap: build attributes, open and end
a span (`BatchSpanProcessor` queues the export), return.

### 8.2 Automated gate (`just check`)

The pure/wiring split makes most assertions cheap — `usage_span_attributes` takes a hand-built
`STTMetrics`/`TTSMetrics`, no session, room, or event loop:

- Attribute-shape per metric type: `usage_details` parses back to exactly
  `{"stt_audio_seconds": ...}` / `{"tts_characters": ...}`; tokens appear only when non-zero;
  `gen_ai.request.model` tracks `metadata.model_name`.
- Zero-usage events produce **no** span (§5.1), using a `_report_connection_acquired`-shaped event.
- Cancelled TTS produces a span *and* counts its characters, tagged `vera.usage.cancelled=true`.
- `vera.usage.source` present on takeover spans, absent on the cascade ones.
- PHI denylist via the existing `assert_no_phi_values(span, ...)` helper.

**The load-bearing test is trace parenting.** Given that §3.3's failure was introduced and caught
during design, it gets a real regression test: open a parent span, capture its context,
`attach_usage_spans(..., parent_context=captured)`, emit the metrics event from a **separate
`asyncio.Task`** (simulating the room-event-callback path), then assert the span's parent is the
captured span and its `trace_id` matches. Nothing else about the output looks wrong when this
breaks, so only a direct assertion catches it.

Span-level assertions use the existing `install_test_tracer_provider()` / `InMemorySpanExporter`
harness (`vera_core/observability/otel_testing.py`) driven by a stub emitter, not a real
`AgentSession`.

**Seeder tests** against a stubbed HTTP client: first run POSTs with no `modelId`; second run
finds the existing entry and threads its `modelId` back in (the specific behavior that avoids the
`(projectId, modelName)` rejection). A missing or unparseable rate env var exits non-zero with
**no** HTTP call issued.

### 8.3 Live verification (definition of done, D6)

Unit tests prove the attributes are on the span; only a live call proves Langfuse's side of the
contract.

1. `just langfuse-up`; set the three rate env vars; `just langfuse-seed-prices`; confirm three
   entries under Settings -> Models.
2. `just up`, `just api`, `just worker`. No telephony needed — the voice-lab browser caller path
   (`caller-` identity) is sufficient.
3. Place a test call; **join as supervisor and Intervene** so the dual-channel takeover STT
   actually runs; fire **hold-to-whisper** once so the coaching path emits. All four attach sites
   exercised in one session.
4. In the trace: **every** `vera.stt.usage` / `vera.tts.usage` observation carries a non-blank
   `$` — checked across all of them, not just the cascade's, since an unseeded model family is
   the one failure that shows up on some spans and not others (§6.1). Takeover spans show both
   `vera.usage.source="rep"` and `="supervisor"`; the whisper span appears in the same Langfuse
   **session** but as its own trace (§3.4).
5. **Check the arithmetic by hand** — `characters x rate ≈ displayed cost`. This is the only step
   that catches a seconds-vs-minutes unit mismatch, which would otherwise render a perfectly
   plausible number that is off by 60x, and it confirms the §5.3 float-seconds decision survives
   ingestion.

Per repo rules, the `/simplify` pass runs before commit and `just check` re-runs afterwards on the
exact tree being committed.

## 9. Open follow-ons (not part of this design's implementation plan)

- PHI-redacting `SpanProcessor` for the pre-existing SDK spans
  (`otel-spans-unredacted-pre-prod`) — schedule before any production cutover.
- Persisting per-call cost to Vera's database as a product-facing field.
- Unifying the coaching-whisper trace with the call's trace via a Redis-stashed remote parent
  (§3.4), if session-level correlation ever proves insufficient.
- Pricing a token-billed STT/TTS provider, should one replace Deepgram/Cartesia — the
  `input_tokens`/`output_tokens` keys already flow through (§5), but the seeder would need
  matching price keys.
