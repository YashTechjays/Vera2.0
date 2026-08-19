# Per-Call Provider Cost Observability in Langfuse — Design

**Date:** 2026-08-17
**Branch:** `feat/tracing-for-stt-and-tts`
**Status:** Approved design, pending implementation plan
**Supersedes:** `2026-07-28-stt-tts-cost-observability-design.md` — that design covered only
STT/TTS, was written against `livekit-agents 1.5.17`, was never implemented, and contains one
load-bearing error (§2.1 below). Its correct findings are carried forward here with attribution.

## 1. Overview

A Vera call spends money at five providers — Deepgram (STT), Cartesia (TTS), Vertex Gemini (the
cascade LLM, the Observer, the health observer, the post-call eval), and OpenAI (fallback tiers).
Today Langfuse can price **only** the cascade LLM. Everything else is either untraced, traced
without usage, or traced into a trace that has nothing to do with the call. There is no query —
UI or API — that answers "what did this call cost."

### Goals

- Emit per-request **usage** for every billed provider surface in Vera, as an observation
  Langfuse's cost engine will actually price.
- Make **one trace per call** the unit of cost aggregation, so a call's total is a number
  Langfuse computes rather than something a human assembles from fragments.
- **Correct the LLM figure that already exists.** Cache hits are measured by the plugin and
  discarded before they reach the span, so today's LLM cost is not merely incomplete — it is
  systematically overstated, and worst on the longest calls (§2.6).
- Keep Vera free of any pricing knowledge (§4 D1).

### Non-goals

- **Redacting the pre-existing LiveKit SDK spans.** `llm_node` / `llm_request` / `tts_node` /
  `user_turn` attach raw transcript and chat-context text (`lk.input_text`, `lk.chat_ctx`,
  `lk.user_transcript`). That is a real gap, tracked as `otel-spans-unredacted-pre-prod`, and
  explicitly deferred by decision on 2026-08-17. **Not widened here** — see §8.
- **Routing `VertexLLMClient` through `ResilientLLM`.** It bypasses the mandated seam because
  the eval needs structured output that `complete()` does not expose. Instrumented where it
  lives; folding structured output into `ResilientLLM` is a separate change (§9).
- **Persisting cost to Vera's own database.** Langfuse's trace rollup is the deliverable.
- **Pricing VAD / EOU / turn detection.** Silero VAD and the turn detector run locally.
- **Wiring the price seeder into the deploy pipeline.** Tracked as a devops-todo row (§7.3).

## 2. Current state (verified 2026-08-17 against `livekit-agents 1.6.7` and the live plugins)

### 2.1 The correction that motivates this redesign

The prior design asserted (its §2.3):

> Cost is computed for **any** observation type; a plain SPAN does not need to be a GENERATION.

**This is false.** Langfuse's token-and-cost documentation states plainly: *"Only observations of
type `generation` and `embedding` can track costs and usage."* Spans are not supported.

Had the prior plan shipped as written, every usage span would have ingested cleanly and rendered
**blank cost** — a failure mode indistinguishable in the UI from broken instrumentation. Every
cost-bearing observation in this design is explicitly typed `generation` (§3.1).

### 2.2 Per-surface state

| # | Surface | Process | Model string | Traced? | Priced? |
|---|---|---|---|---|---|
| 1 | Cascade STT | worker | `flux-general-en` | **no span at all** | no |
| 2 | Takeover per-track STT | worker | `nova-3` | no | no |
| 3 | Whisper STT (`ResilientSTT`) | control plane | `flux-general-en` | no | no |
| 4 | Cascade TTS | worker | `sonic-3.5-2026-05-04` | span, usage unreadable | no |
| 5 | Cascade LLM | worker | `gemini-2.5-flash` | yes, `gen_ai.usage.*` | yes, but **overstated** (§2.6) |
| 6 | Observer extract | worker | `gemini-3.5-flash` | yes (auto) | §2.6, §7.2 |
| 7 | Health observer | worker | `gemini-3.1-flash-lite` | yes (auto) | §2.6, §7.2 |
| 8 | Call summary | control plane | `gemini-3.1-flash-lite` | yes, but an **orphan root trace** | §2.6, §7.2 |
| 9 | **Post-call eval** (extract + judge) | control plane | `gemini-2.5-flash` | **zero spans** | no |

### 2.3 STT — usage exists in-process and reaches nothing

`stt/stt.py` in 1.6.7 contains **zero** tracing: no span, no attribute, no tracer import. Usage
leaves the module only via `self.emit("metrics_collected", stt_metrics)`. `telemetry/otel_metrics.py`
forwards it to the OTel **Metrics** API, but `vera_core/observability/otel.py` configures only a
`TracerProvider` — so LiveKit gets OTel's default **no-op** meter and the numbers are discarded
in-process.

**Installing a `MeterProvider` would not help:** Langfuse's OTLP endpoint ingests traces only.
This is why the fix is span-based for STT too.

### 2.4 TTS — a span whose usage Langfuse's cost engine cannot read

TTS does get spans (`tts_node`, `tts_request`), and `tts/tts.py:394,753` attaches
`lk.tts_metrics = metrics.model_dump_json()` carrying a real `characters_count`. But
`lk.tts_metrics` is an arbitrary custom attribute bag, not something Langfuse parses for usage.
The SDK sets the standardized `gen_ai.usage.*` names **only** on LLM/realtime spans — grep
confirms `usage_details` appears nowhere in the SDK at all. Hence LLM prices and TTS does not,
side by side in the same trace.

### 2.5 Post-call eval — the genuinely untraced LLM spend

`control_plane/llm.py` `VertexLLMClient` calls `genai.Client(...).aio.models.generate_content(...)`
directly — the raw Google SDK, which emits no OTel spans. It is reached by both `extract()` and
`judge()`, and `judge()` fans out into concurrent chunks (`_JUDGE_CHUNK_SIZE`), so a single form
can drive many Vertex calls that are today entirely invisible.

### 2.6 LLM cache hits are measured, then discarded — so LLM cost is overstated

Every LLM surface is affected, including the one surface §2.2 marks as already working.

The chain is measured end to end and then dropped at the last step:

1. The Google plugin reports real cache hits: `prompt_cached_tokens=usage.cached_content_token_count or 0`
   (`plugins/google/llm.py:522`).
2. The SDK collects it into `LLMMetrics.prompt_cached_tokens` (`llm/llm.py:364`), and documents
   that `prompt_tokens` **"includes cached tokens"** (`llm/llm.py:39`).
3. The `llm_request` span then sets **only** `ATTR_GEN_AI_USAGE_INPUT_TOKENS` and
   `ATTR_GEN_AI_USAGE_OUTPUT_TOKENS` (`llm/llm.py:378-390`). `ATTR_GEN_AI_USAGE_INPUT_CACHED_TOKENS`
   is defined in `telemetry/trace_types.py` but set **only** on the realtime-model path
   (`telemetry/utils.py:41`) — which Vera does not use; Vera runs the cascade.

**Consequence:** Langfuse receives one undifferentiated input-token count that silently includes
the cache hits, and prices all of it at the full input rate. Gemini bills cached input at a small
fraction of that, so every LLM figure Vera shows today is **overstated**, and the error is not a
rounding artifact — it scales with the cache hit rate.

It is worst exactly where Vera spends most. The cascade re-sends a growing chat context on every
turn, which is the archetypal implicit-cache workload, so the longer the call the larger the
overstatement. Vera does not pass `cached_content` (`cascade.py:142-145`), so it relies on Gemini's
**implicit** caching — nothing is configured or opt-in, the hits simply happen and go unreported.

This is a correctness bug in the existing cost figure, not merely a missing breakdown, and it
cannot be fixed by adding a Vera-owned span: a second generation for the same request would be
summed by Langfuse, double-counting the call. The span must be corrected **in place** (§3.6).

### 2.7 One uniform hook for STT and TTS

`STTMetrics` and `TTSMetrics` both flow through `metrics_collected`, and both carry exactly what
billing needs — the SDK's own docstrings say so:

| | field | SDK docstring |
|---|---|---|
| `STTMetrics` | `audio_duration: float` | "The duration of the pushed audio in seconds." Measured locally by the plugin, not reported by Deepgram; flushed per ~5s chunk (§5.3) |
| `TTSMetrics` | `characters_count: int` | "Number of characters synthesized (for character-based billing)." |
| both | `input_tokens` / `output_tokens: int` | "for token-based billing" — 0 for Deepgram + Cartesia |
| both | `metadata: Metadata \| None` | `model_name` / `model_provider` |

**`metadata` is populated by the SDK base class, not the plugins** (`stt/stt.py:228,448,525`,
`tts/tts.py`), reading each plugin's `model` / `provider` properties. Verified: all three plugins
Vera uses implement them, and `model` returns the literal string Vera passed —
`deepgram/stt_v2.py:179` → `flux-general-en`, `deepgram/stt.py:229` → `nova-3`,
`cartesia/tts.py:207` → `sonic-3.5-2026-05-04`; `provider` returns the constants `"Deepgram"` /
`"Cartesia"`. This gives the seeder's `matchPattern` an exact, predictable target.

Vera has **no** `metrics_collected` listener today, so this is new wiring rather than a change to
existing behavior.

### 2.8 What Langfuse accepts (the enabling finding)

- `langfuse.observation.usage_details` — a JSON-string attribute with **arbitrary keys**,
  whose keys are matched *exactly* against the matched model definition's per-usage-type prices and
  summed. Non-token billing units are fully supported: *"Usage types can be arbitrary strings and
  differ by LLM provider."* **Values must be non-negative integers** — the storage column is
  `Map(LowCardinality(String), UInt64)` and fractions are truncated or dropped depending on the
  ingestion route (§5.3). The prior design asserted the opposite; that error is corrected there.
- `langfuse.observation.type` — `"span" | "generation" | "event"`, default `"span"`. Cost is
  computed only for `generation` (and `embedding`) — see §2.1.
- Attribute precedence is `langfuse.*` > `gen_ai.*` > framework-specific > generic.
- Model matching is a **regex** (`matchPattern`) against the observation's `model` field.

## 3. Components

### 3.1 The generation contract — `vera_core/observability/usage_spans.py` (new)

Every priced observation Vera emits carries this shape:

```
langfuse.observation.type           = "generation"        # explicit — see below
langfuse.observation.model.name     = "flux-general-en"   # langfuse.* wins on precedence
gen_ai.request.model                = "flux-general-en"   # OTel semantic convention
gen_ai.provider.name                = "Deepgram"
langfuse.observation.usage_details  = '{"stt_audio_ms": 27640}'
<call_trace_attributes(room_name)>                        # vera.room, vera.tenant_id,
                                                          # vera.call_id, langfuse.session.id
```

**The type is set explicitly rather than inferred.** Langfuse also promotes "any span with a
`model` attribute" to a generation, but that is an inference rule that has already changed once;
relying on it would make our cost pipeline depend on undocumented promotion behavior. Setting the
attribute costs nothing and is self-documenting.

Two public functions, split so the part Langfuse contracts on is testable without booting anything:

```python
def usage_span_attributes(metrics: Any) -> dict[str, str | int | float | bool] | None:
    """Exact generation attributes for one STT/TTS metrics event, or None when there
    is no billable usage (§5.1). Pure — no OTel, no I/O."""

def attach_usage_spans(
    emitter: Any,
    *,
    parent_context: Context | None = None,
    room_name: str | None = None,
    source: str | None = None,
) -> None:
    """Register a metrics_collected listener emitting one generation per billable event."""
```

`attach_usage_spans` accepts any `rtc.EventEmitter` emitting `metrics_collected`, which covers all
four speech surfaces with no per-site special-casing.

### 3.2 Cross-process trace join — `vera_core/observability/trace_link.py` (new)

This is what makes the aggregate number real, and it is the main departure from the prior design.

**Why a single trace, not a session.** The prior design correlated everything with
`langfuse.session.id = room_name` and relied on the session rollup for a call total. Langfuse issue
**#15109 (open)** reports session "Total cost" rendering **$0.00 when cost is model-calculated
rather than caller-ingested** — precisely our case, since D1 keeps prices in Langfuse. Trace-level
and Metrics-API rollups are unaffected. So the call total must be a **trace** total.

**Why propagate rather than derive.** A deterministic trace id derived from the room name (e.g.
`sha256(room_name)[:16]`) would need no shared state, but LiveKit creates the `job_entrypoint` span
before Vera's code runs, and every valuable auto-span (`llm_request` with `gen_ai.usage.*`,
`tts_request`) hangs beneath it. A derived id would produce a *second* trace alongside it and
defeat the purpose. The worker's trace id is whatever LiveKit minted; it must be carried.

**Mechanism:**

- **Worker, at the entrypoint** (where `job_entrypoint` is provably ambient): serialize the current
  context to a W3C `traceparent` and write `vera:trace:<room_name>` to Redis. Follows the existing
  per-call key convention (`vera:call-plan:<room>`, `vera:summary:<room>`,
  `vera:call-events:<room>`); `plan_store.py` is the closest structural precedent for a
  worker↔control-plane per-call blob.
- **Control plane:** read the key, rebuild a remote-parent `Context` (a `NonRecordingSpan` over the
  decoded `SpanContext`, `is_remote=True`), and open the whisper / summary / post-call-eval spans
  under it. Same `trace_id` → the call's trace total includes them.
- **Fallback:** key absent or expired → own root trace plus `langfuse.session.id`, i.e. exactly
  today's behavior. Degrades, never fails a request.

A `traceparent` is random hex — PHI-free by construction (§8).

**TTL.** Sized to cover the longest realistic post-call window, not the call: post-call eval
normally runs seconds after `call.ended`, but the pipeline sweeper can re-drive a stranded job
minutes later. 24h is generous, and one small key per call is negligible.

### 3.3 The eight attach sites

| # | Site | Emitter / chokepoint | Trace placement |
|---|---|---|---|
| 1 | `cascade.build_session` | `deepgram.STTv2` | under `job_entrypoint`, captured context |
| 2 | `cascade.build_session` | `cartesia.TTS` | under `job_entrypoint`, captured context |
| 3 | `main.py` takeover `stt_factory`, per track | `deepgram.STT` | under `job_entrypoint` via closure |
| 4 | `vera_core/stt.py` `ResilientSTT._adapter()` | the `FallbackAdapter` | call's trace via §3.2 |
| 5 | `control_plane/llm.py` `VertexLLMClient._generate` | the Vertex call itself | call's trace via §3.2 |
| 6 | (same chokepoint — judge fan-out) | one generation per chunk | call's trace via §3.2 |
| 7 | `call_summary.summarize_call` | `ResilientLLM` (already auto-spanned) | call's trace via §3.2 |
| 8 | `configure_observability` exporter wrapper | every SDK `llm_request` span | in place — corrects §2.6 for all LLM surfaces at once |

`build_session` currently constructs `stt=` / `tts=` inline as `AgentSession` kwargs
(`cascade.py:139,148`); they must be bound to locals so listeners can attach. The cascade LLM needs
nothing — it already prices.

**Site 4 attaches to the `FallbackAdapter`, not to each inner STT.** STT's `FallbackAdapter`
re-emits `metrics_collected` verbatim (`stt/fallback_adapter.py:294-295`), so `metadata.model_name`
stays the true provider model rather than the literal `"FallbackAdapter"`. One listener on
`self._chain` covers the chain. `aclose()` discards the chain, so a rebuilt chain gets a fresh
listener — no leak, no double registration.

**Sites 5-6 share one chokepoint.** `_generate` is called by both `extract()` and `_judge_chunk()`,
so wrapping it once instruments both, and the judge's concurrent chunks each get their own
generation — the trace shows the real fan-out cost rather than one aggregate. Token counts come
from the response's `usage_metadata`; a response that carries none emits no usage rather than
inventing zeros.

### 3.4 Dual-channel takeover attribution

A takeover with both the callee's and an intervening supervisor's audio produces two
`_transcribe_track` tasks and therefore two STT instances (`takeover_transcript.py:134`), both
billed. `stt_factory` is currently `Callable[[], agents_stt.STT[Any]]` (`:92`) — no arguments — so
a span cannot say which channel it billed.

Widen it to `Callable[[SpeakerAttribution], agents_stt.STT[Any]]` and pass `attribution.source`
through as `vera.usage.source`. `SpeakerAttribution.source` is a `TurnSource` closed enum
(`SOURCE_REP` / `SOURCE_SUPERVISOR`), so it is PHI-safe per §8. `attribution.user_id` is **not**
attached — it adds nothing to cost. This is the only change to `takeover_transcript.py`.

### 3.5 Trace parenting inside the worker — why context is passed explicitly

`job_entrypoint` wraps Vera's entrypoint function, so it is the ambient span inside `main.py` —
which is why `main.py:387`'s `trace.get_current_span().set_attributes(...)` already works.

The takeover STT is reached by **two paths with different contexts**:

| Path | Context |
|---|---|
| `start()` → `_maybe_transcribe` — tracks already subscribed, called synchronously from the entrypoint | inherits `job_entrypoint` |
| `room.on("track_subscribed")` → `_on_track_subscribed` — tracks subscribed later | inherits whatever task LiveKit's room event dispatch runs in |

A supervisor who joins **after** takeover begins goes down path 2. Reading `context.get_current()`
at emit time there would capture an unrelated context and the spans would become **new trace
roots** — falling out of the call's trace and never summing into its cost, with nothing else about
the output looking wrong.

**Therefore:** capture the context **once in the job entrypoint** and let the closure carry it. A
closure over a value is immune to whichever task later invokes it. This is why `attach_usage_spans`
takes `parent_context` explicitly rather than sniffing ambient context.

### 3.6 Correcting the SDK's LLM spans in place — an exporter wrapper

§2.6 is a defect in a span Vera does not own, so it cannot be fixed by attaching a listener or
emitting a sibling span. Two properties make an in-place correction possible:

- The SDK already puts everything needed on the span: `lk.llm_metrics` is
  `LLMMetrics.model_dump_json()`, carrying `prompt_tokens`, `prompt_cached_tokens` and
  `completion_tokens`.
- **`langfuse.*` attributes take precedence over `gen_ai.*`** (§2.8). So adding
  `langfuse.observation.usage_details` to the *same* span overrides the SDK's uncorrected
  `gen_ai.usage.*` — one generation, corrected usage, no double count.

`configure_observability` therefore wraps the OTLP exporter:
`BatchSpanProcessor(UsageEnrichingExporter(OTLPSpanExporter(...)))`. For each span carrying
`lk.llm_metrics`, the wrapper re-exports it with a corrected `usage_details` (§5.5) and an explicit
`langfuse.observation.type`. Every other span passes through untouched.

**This is the one place D10's rejection does not apply.** Export-time rewriting was rejected as the
*mechanism for STT/TTS*, because no STT span exists to rewrite — there it would fix a fraction of
the problem and leave two mechanisms to maintain. Here the span exists, is already correct in every
respect except one, and is not ours to change at the source. The wrapper is additive and keyed on a
single attribute, so a span without `lk.llm_metrics` is untouched and an SDK upgrade that renames
the attribute degrades to today's behavior rather than corrupting anything.

An exception anywhere in the wrapper must fall through to exporting the **original** span rather
than dropping it: losing a span is worse than exporting an uncorrected one.

**PHI note:** the wrapper *reads* `lk.llm_metrics` and writes three integers. It never copies the
blob onto a span — §8 prohibition 1 stands.

### 3.7 Known gap: a re-dispatched call splits into two traces

The link is a plain `SET`, so it is last-write-wins. That is correct for the normal
case — LiveKit dispatches a room to exactly one worker, so there is a single writer per
call — but it leaves one gap.

If a worker dies mid-call and LiveKit re-dispatches the same room, the new job mints a
**new** `job_entrypoint` span, and its publish overwrites the traceparent. Every
control-plane span afterwards (post-call eval, summary, whisper) joins the second
trace. The first attempt's STT, TTS and LLM spend stays in the first trace, which
nothing now points at, so **the call under-reports its cost by whatever the first
attempt spent**.

Accepted rather than fixed, because the alternatives are worse than the gap:

- `SETNX` would pin the call to the *dead* worker's trace, so every later span would
  join a trace whose root never completed — a worse reading than a short one.
- Appending both traceparents would need a per-call cost query that unions traces,
  which is exactly the per-trace rollup this design exists to rely on.

The gap is bounded (it needs a mid-call worker death) and self-announcing: the first
trace has a `job_entrypoint` with no control-plane spans beneath it, and the second has
a suspiciously short STT duration for the call's wall-clock length. `just
langfuse-verify` does not detect it. If re-dispatch ever becomes common, revisit by
having the control plane sum over `vera.room` rather than over the trace id.

## 4. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Who owns per-unit prices | **Langfuse model config.** Vera sends raw `usage_details` and holds no rate anywhere. A price change never needs a Vera deploy; the cost is that each environment must be seeded (§7). |
| D2 | Observation type | **Explicitly `generation`** on every cost-bearing span (§2.1, §3.1). |
| D3 | Cost aggregation unit | **One trace per call**, joined cross-process (§3.2). Session id is still stamped, but not depended on for cost. |
| D4 | Where usage lands | **One Vera-owned generation per billable event** — per-turn attribution; Langfuse sums to the trace total. |
| D5 | Which speech hook | **Component-level `emitter.on("metrics_collected", ...)`**. `session.on("metrics_collected")` is deprecated and warns per registration; the component emitters are what the SDK itself uses. Rejected: `session_usage_updated`, whose usage is cumulative per `(provider, model)`, has discarded per-request identity, and cannot see the takeover STT. |
| D6 | Billing units | **Integer milliseconds / integer characters / integer tokens** (§5.3). Langfuse stores usage as `UInt64`, so fractional seconds truncate — and because STT usage arrives per ~5s chunk, that error would recur on every event and always downward. Vera rounds to an integer at emit time so the value never depends on ingestion-route semantics. |
| D7 | Price seeding | **Idempotent script** + `just` recipe + manual runbook + devops-todo row (§7). |
| D8 | Definition of done | **Live end-to-end verification**, not unit tests alone (§10.3). |
| D9 | Post-call eval seam | **Instrument in place.** Not routed through `ResilientLLM` (§1 non-goals, §9). |
| D10 | Rewriting spans at export time **as the STT/TTS mechanism** | **Rejected.** Reading `lk.tts_metrics` in an exporter wrapper would price TTS with no new spans, but no span anywhere carries STT usage — it fixes a fraction of the problem and leaves two mechanisms to maintain. |
| D11 | Fixing the LLM cached-token defect (§2.6) | **Export-time correction of the SDK's own `llm_request` span** (§3.6). A Vera-owned sibling generation would be summed by Langfuse and double-count the request; the span is not ours to fix at the source; and `langfuse.*` beating `gen_ai.*` makes an in-place override exact. Narrow exception to D10, for the opposite reason D10 exists. |
| D12 | Cached tokens as a usage key | **Reported separately as `cached`, with `input` reduced by it.** Sending `prompt_tokens` whole alongside `cached` would double-count (§5.4, §5.5). Requires a `cached` price on the Gemini entries (§7.2) — without one, cache hits price at $0 and cost is *understated* instead. |

## 5. Attributes and data shape

Span names: **`vera.stt.usage`**, **`vera.tts.usage`**, **`vera.eval.generate`**.

```
# vera.tts.usage — one TTS request
langfuse.observation.type           = "generation"
langfuse.observation.model.name     = "sonic-3.5-2026-05-04"
gen_ai.request.model                = "sonic-3.5-2026-05-04"
gen_ai.provider.name                = "Cartesia"
langfuse.observation.usage_details  = '{"tts_characters": 465}'
vera.usage.streamed                 = true
vera.usage.audio_seconds            = 27.64      # operational only, NOT priced
<call_trace_attributes(room_name)>

# vera.stt.usage — one recognition-usage event (cascade: no source attribute)
langfuse.observation.type           = "generation"
langfuse.observation.model.name     = "flux-general-en"
gen_ai.provider.name                = "Deepgram"
langfuse.observation.usage_details  = '{"stt_audio_ms": 27640}'
vera.usage.streamed                 = true

# vera.stt.usage — takeover per-track variant, which alone carries a source
langfuse.observation.model.name     = "nova-3"
langfuse.observation.usage_details  = '{"stt_audio_ms": 12100}'
vera.usage.source                   = "supervisor"

# vera.eval.generate — one Vertex call (extract, or one judge chunk)
langfuse.observation.type           = "generation"
langfuse.observation.model.name     = "gemini-2.5-flash"
gen_ai.provider.name                = "google"
langfuse.observation.usage_details  = '{"input": 8412, "output": 611, "cached": 0}'
vera.eval.pass                      = "extract" | "judge"
<call_trace_attributes(room_name)>
```

`input_tokens` / `output_tokens` are folded into a speech `usage_details` **only when non-zero**.
Deepgram and Cartesia report 0; a zero-valued key would demand a price entry for a unit nobody
bills, and if a token-billed provider is ever adopted the keys appear automatically.

### 5.1 Zero-usage events are dropped

`stt/stt.py:439` emits a genuine `STTMetrics` with `audio_duration=0.0` and `request_id=""` purely
to report websocket connection timing. A span for it would add a `$0` noise generation per connect.
Rule: `usage_span_attributes` returns `None` when every billable quantity is zero — STT when
`audio_duration == 0 and input_tokens == 0 and output_tokens == 0`, TTS when `characters_count == 0`
and both token counts are zero.

### 5.2 Cancelled TTS still counts — but the `cancelled` flag is NOT reported

Characters handed to the synthesizer are billed whether or not the request was torn down, so the
generation **is** emitted and the characters **are** counted. Suppressing them would under-report
real money. That part stands.

**Corrected 2026-08-18 — `TTSMetrics.cancelled` does not mean "barge-in", and is deliberately not
put on the span.** The original text here assumed it flagged interruptions and tagged spans
`vera.usage.cancelled=true` so barge-in spend stayed queryable. It does not, for a structural
reason:

- `cancelled=self._task.cancelled()` (`tts.py:744`) reads the **stream's** main task, created once
  per `SynthesizeStream` (`tts.py:581`);
- but `_emit_metrics()` fires **per segment**, on each `ev.is_final` (`tts.py:771`);
- and `aclose()` calls `cancel_and_wait(self._task)` (`tts.py:821`) *before* awaiting
  `self._metrics_task` (`tts.py:826`), so any segment flushed during teardown sees an
  already-cancelled task.

A stream-scoped flag read at a segment-scoped emit point, with teardown ordered to guarantee it is
set. **Measured on a real 17-minute call: 69 of 69 TTS requests reported `cancelled=true`, against
1–2 actual interruptions.** A field that is always true reads as signal, and it did mislead — it
produced a "100% of TTS characters billed on cancelled requests" finding that looked like an
over-billing bug and was an artifact. Worse than absent, so it is now absent.

There is no interruption signal available from `TTSMetrics`. If barge-in spend needs to be
queryable, it has to come from the session's interruption events, not from here. Re-check
`tts.py` after any `livekit-agents` bump in case the field is rescoped.

### 5.2a Thinking-token accounting — verified against Vertex, 2026-08-18

The residual (`total_tokens - prompt_tokens - completion_tokens`) is only a valid stand-in for
thoughts if the provider's total actually includes them. Probed directly rather than inferred:

```
gemini-2.5-flash, default thinking      prompt=18 candidates=103 thoughts=1324 total=1445
gemini-2.5-flash, thinking_budget=1024  prompt=18 candidates=90  thoughts=925  total=1033
```

`total - prompt - candidates` equals `thoughts_token_count` **exactly** in both cases, so the
residual is the thoughts count and the mechanism is sound.

The practical consequence is the useful half: because the total includes thoughts, a residual of
**zero is positive evidence that no thoughts were produced** — not evidence that we failed to
observe them. On the first live validation call (trace `8b7d88ec…`) the summed residual was 0
across all 375 `llm_request` spans, and that call is therefore fully accounted for, not
under-reported. The cascade ran at `thinking_level='minimal'` (a tenant override; the code default
for a Gemini 3 model is `low`), and the `ResilientLLM` surfaces produced none either.

**Cost warning for whoever tunes this.** Thinking bills at the OUTPUT rate, and the ratio is not
small: the default-thinking probe above spent 1324 thought tokens against 103 candidate tokens —
92% of billed output. Raising `thinking_level` on the cascade can move LLM output cost by roughly
an order of magnitude with nothing else about the trace looking different. `vera.llm.thinking_tokens`
on the corrected span is where that shows up; watch it, not the token total.

### 5.3 STT units: integer milliseconds — Langfuse cannot store fractions

**Langfuse's usage values are integers, end to end.** Verified 2026-08-17 against the Langfuse
source, not inferred from docs:

- ClickHouse stores `observations.usage_details` as **`Map(LowCardinality(String), UInt64)`** — an
  unsigned integer map. A fractional value is **truncated** at insert.
- The public API declares `usageDetails` as `map<string, integer>`, and
  `IngestionService.normalizeProvidedUsageDetails` runs every value through `Number(value)`.
- On the SDK/API ingestion route, `RawUsageDetails` keeps only values satisfying
  `Number.isInteger(value) && value >= 0` — a float there is **dropped entirely**, not rounded.
  The OTel attribute route (`langfuse.observation.usage_details`) bypasses that Zod validation and
  JSON-parses the object as-is, so a float would survive to the `UInt64` insert and be truncated.

**The prior design's premise was wrong.** It asserted ingestion validated
`z.record(z.string(), z.number().nonnegative())` and therefore "floats are accepted"; that
validator does not govern this field. Seconds-with-decimals was never a safe unit.

**Truncation would be severe here, not cosmetic — because of how the usage arrives.** Deepgram does
not report usage at all; the LiveKit plugin measures it locally, summing
`AudioFrame.duration` (`samples_per_channel / sample_rate`) into a
`PeriodicCollector(duration=5.0)` that flushes **roughly every 5 seconds** of audio
(`plugins/deepgram/stt_v2.py:310`, `stt.py:483`). So a call emits one usage event per ~5s chunk,
each a float sum of frame durations.

Truncation therefore lands on **every event**, and it always floors:

| Unit | A ~5s chunk arrives as | Stored | Error |
|---|---|---|---|
| seconds | `4.999999999999998` | `4` | **−20% on that event** |
| seconds | final flush remainder `0.4` | `0` | **lost entirely** |
| milliseconds | `4999.999999999998` | `4999` | −0.02% |

Floating-point summation makes `4.999…` at least as likely as `5.000…`, so the seconds error is
not a rare edge — it is a systematic downward bias across most events of every call, and silent.

**Decision: `stt_audio_ms`, integer milliseconds, rounded in Vera at emit time.** Rounding in Vera
rather than relying on Langfuse's truncation makes the value an integer *by construction*, so the
design never depends on which ingestion route it takes — it satisfies the strict
`Number.isInteger()` path and the `UInt64` column equally.

The cost is an awkward per-millisecond rate (~`$0.00000012833` for Nova-3 at $0.0077/min), which is
comfortably representable: model prices are Postgres `Decimal`/NUMERIC (arbitrary precision) and
costs are ClickHouse `Decimal64(12)`, whose floor is `1e-12` — one 5s event costs ~`6.4e-4`. The
real hazard is operational, not numeric: **entering a per-second or per-minute rate into a
per-millisecond field is a 1,000× or 60,000× error**, so §7.1 and the runbook name the unit in the
env var itself and §10.3 step 6 checks the arithmetic by hand.

Cartesia bills **per character** (1 credit per character; ~$5-37 per million, plan-dependent) and
`characters_count` is already an `int`, so `tts_characters` needs no unit conversion. Token counts
are likewise integers. **STT is the only surface whose unit changes.**

### 5.4 Vertex token mapping for the eval generations

Google's `usage_metadata` does not map field-for-field onto billable usage, and getting this wrong
mis-bills silently in either direction:

| `usage_metadata` field | Maps to | Why |
|---|---|---|
| `prompt_token_count` | `input`, **minus** `cached_content_token_count` | The prompt count **includes** cached tokens; sending it whole while also sending `cached` double-counts them |
| `cached_content_token_count` | `cached` | Priced separately (and usually cheaper) when the model entry carries the key |
| `candidates_token_count` **+** `thoughts_token_count` | `output` | Thinking tokens are billed as output, and `gemini-2.5-flash` — the eval model — is a thinking model that Vera configures thinking on |
| `total_token_count` | *unused* | Derivable, and sending it would triple-count |

**The keys are `input` / `output` / `cached` deliberately.** Langfuse maps the SDK's
`gen_ai.usage.input_tokens` / `output_tokens` onto exactly these usage keys, so the eval
generations and the cascade's auto-instrumented `llm_request` spans share one vocabulary — and
therefore one seeded model entry prices both. Inventing `eval_input_tokens` would need a second,
parallel price entry for the same model.

A response carrying no `usage_metadata` emits the span with **no** `usage_details` rather than
zeros — a zero-cost generation is indistinguishable from a broken one (§7.1's reasoning applied
to instrumentation).

### 5.5 The corrected LLM usage split (§3.6 exporter wrapper)

The same three-key vocabulary, derived from the `lk.llm_metrics` blob already on the span:

| `LLMMetrics` field | Maps to | Why |
|---|---|---|
| `prompt_tokens` **minus** `prompt_cached_tokens` | `input` | The SDK documents `prompt_tokens` as *"includes cached tokens"* (`llm/llm.py:39`); sending it whole alongside `cached` double-counts the hits |
| `prompt_cached_tokens` | `cached` | The number the SDK measures and then discards (§2.6) |
| `completion_tokens` | `output` | Unchanged |

```
# llm_request — the SDK's own span, corrected at export
langfuse.observation.type           = "generation"
gen_ai.request.model                = "gemini-2.5-flash"   # SDK-set, untouched
gen_ai.usage.input_tokens           = 12480                # SDK-set, now overridden
langfuse.observation.usage_details  = '{"input": 3120, "cached": 9360, "output": 210}'
```

`input + cached` always reconstructs the SDK's original `prompt_tokens`, which is the invariant the
unit test asserts — it catches both the double-count (if the subtraction is dropped) and an
under-count (if `cached` is omitted while `input` is reduced).

A span whose `prompt_cached_tokens` is 0 still gets `usage_details`, with `cached` omitted rather
than sent as 0 — consistent with §5's rule that a zero-valued key demands a price entry for a unit
nobody billed on that request. The correction is then a no-op in value but keeps one code path.

## 6. Error handling

Every attach and every span emit is wrapped so a tracing failure can never affect the call, the
transcript, the whisper request, or the post-call job: `try/except Exception` →
`logger.warning(..., type(exc).__name__)`. Never a bare `except` that would swallow
`asyncio.CancelledError`. Log the exception **type name only**, never its repr or traceback, per
`phi-safe-exception-logging` discipline — a provider error can embed the request payload.

Speech listeners are invoked synchronously from `rtc.EventEmitter.emit`, so the handler stays sync
and cheap: build attributes, open and end a span (`BatchSpanProcessor` queues the export), return.

The Redis trace-link write and read are both best-effort: a write failure logs and proceeds (the
call is unaffected); a read failure or miss falls back to a root trace (§3.2).

## 7. Price seeding

### 7.1 `scripts/seed_langfuse_prices.py` + `just langfuse-seed-prices`

Follows the `scripts/bootstrap_platform_admin.py` pattern: idempotent, env-driven, run-on-demand.

- **Auth needs no new config** — basic auth from the existing `langfuse_public_key` /
  `langfuse_secret_key` / `langfuse_host` in `Settings`. Logs the target host; never the secret.
- **Rates come from script-scoped env vars, not `Settings`** — following the `SEED_ADMIN_EMAIL`
  precedent. The application never needs a price, so this keeps exactly one place prices live with
  no drifting second copy inside Vera. Reinforces D1.

| `modelName` | `matchPattern` | usage key | rate env var |
|---|---|---|---|
| `vera-deepgram-flux` | `(?i)^flux-.*$` | `stt_audio_ms` | `LANGFUSE_PRICE_STT_FLUX_PER_MS` |
| `vera-deepgram-nova` | `(?i)^nova-.*$` | `stt_audio_ms` | `LANGFUSE_PRICE_STT_NOVA_PER_MS` |
| `vera-cartesia-sonic` | `(?i)^sonic-.*$` | `tts_characters` | `LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER` |

The env var names carry the unit (`_PER_MS`) because the rate is a small, hard-to-eyeball number:
a Deepgram per-millisecond rate is ~`$0.00000012833`, and entering the published per-minute figure
(`$0.0077`) instead would overstate cost by 60,000× while still rendering a plausible dollar
amount. The seeder logs each rate next to its unit for the same reason.

Prices go in via `pricingTiers[0].prices`, **not** the deprecated flat
`inputPrice`/`outputPrice`/`totalPrice`, which cannot express a custom usage key at all.

**Family patterns, not exact versions.** `(?i)^sonic-.*$` matches the pinned
`sonic-3.5-2026-05-04` today and survives a bump to `sonic-4`. An exact pattern would silently
zero the cost on the next model change, and a missing match renders identically to "no cost data,"
making that failure invisible. Tradeoff: if two versions in a family are priced differently, the
family entry is wrong for one; Langfuse's `startDate` handles that when it happens.

**Idempotency has a specific shape.** `POST /api/public/models` upserts *only* when passed an
existing `modelId`; a duplicate `modelName` without one is rejected on the
`(projectId, modelName)` uniqueness check. So the script GETs the model list, matches by
`modelName`, and threads the existing `modelId` back into the POST.

**It refuses to seed a zero.** A missing or unparseable rate env var exits non-zero having written
nothing — a `$0.00` entry is indistinguishable from broken instrumentation, so a partial seed is
worse than no seed.

**Real rates are a human input.** The public list prices quoted in §5.3 are a sanity reference
only, never the contracted rate. Because the script demands the env vars, no placeholder price can
ship in code.

The script writes to whatever `VERA_LANGFUSE_HOST` resolves to, so running it with production env
targets production. It is config-only, idempotent and non-destructive, so no confirmation prompt —
just the target-host log line.

### 7.2 LLM model entries and the cached-token rate

Vera's LLM selectors are settings-driven and several are recent enough that Langfuse's built-in
price table may not cover them: `gemini-3.1-flash-lite` (summary, health), `gemini-3.5-flash`
(observer extract), `gemini-2.5-flash` (cascade, post-call eval), `gpt-5.4-mini` (fallback tiers),
plus `assemblyai:best` for whisper STT if that key is ever provisioned. These spans exist today,
so an uncovered model is already silently rendering blank cost.

**Whatever entry serves a Gemini model must carry all three price keys — `input`, `output` and
`cached`.** This is the half of §2.6 that lives outside Vera: the instrumentation can report cache
hits perfectly and still produce a wrong total if the model entry prices only `input` and `output`,
because the separated `cached` tokens would then price at $0. That failure is the mirror image of
today's — cost *understated* rather than overstated — and just as invisible.

A built-in Langfuse entry that predates custom usage keys may well price only `input`/`output`. The
seeder therefore owns explicit `vera-gemini-*` entries for the configured models rather than
relying on built-ins, so the `cached` rate is guaranteed present and the three keys stay in one
place. The rates come from env vars on the same refuse-to-seed-a-zero terms as the speech rates
(§7.1), except that a **missing `cached` rate is fatal too** — silently omitting it is exactly the
understatement above.

The seeder therefore **logs every configured model selector that matches no entry** (built-in or
seeded) after it runs. It does not invent prices for them — that stays a human decision — but the
gap becomes a log line instead of a blank column. §10.3 step 4 checks cost is non-blank on
*every* generation, not just the speech ones, for the same reason.

### 7.3 Runbook and devops-todo

- `docs/superpowers/specs/2026-08-17-langfuse-price-entry-runbook.md` — the by-hand fallback:
  the click path (Settings → Models → + New model), which UI field maps to which API field, the
  entries as a fill-in table, **how to discover the usage key from a live generation** rather than
  trusting the doc, and a "cost is blank — why" triage table (no model entry · pattern does not
  match · price key ≠ usage key · usage key typo'd in instrumentation), since all four look
  identical in the UI. Opens by pointing at `just langfuse-seed-prices` as the preferred path so
  it does not quietly become the primary route and drift.
- `adr/devops-todo.md` gains a row: seed the price entries in every environment, re-run after any
  Langfuse project re-provision (the entries live in Langfuse's DB, not this repo) and after any
  model-family change. Deliberately **not** wired into the deploy path — it is Langfuse-side config
  that changes about once per environment, and folding it in would ship rate env vars somewhere
  that otherwise never needs them.

## 8. PHI guardrail (hard requirement)

The repo rule is unconditional: never log, print, trace, or attach to a span plaintext PHI
(`vera-backend/CLAUDE.md`, enforced by a PreToolUse hook). The pre-existing SDK spans already
violate it (§1 non-goals) — accepted, tracked, deferred, and **not** a license to add more.

Neither `STTMetrics` nor `TTSMetrics` carries any text field; `characters_count` is
`len(input_text)`, a length. Every attribute in §5 is one of:

- a count or duration (`characters_count`, `audio_duration`, token counts)
- a boolean (`streamed`)
- a closed enum (`vera.usage.source` → `TurnSource`; `vera.eval.pass`; `gen_ai.provider.name`)
- a fixed model name Vera itself passed in
- the existing `call_trace_attributes` set (room name + tenant/call UUIDs)
- a W3C `traceparent` — random hex identifiers only

**Never**, anywhere in this implementation: transcript text, `SpeechEvent.alternatives[0].text`,
extracted answer values, DTMF digits, or — new to this design and the sharpest edge here — the
**post-call eval's prompts or completions**. `build_extract_prompt` / `build_judge_prompt` embed
the full transcript, and the response carries extracted answer values. The `vera.eval.generate`
span carries token *counts* and nothing else.

**Three explicit prohibitions beyond the allow-list:**

1. Do **not** copy the SDK's `metrics.model_dump_json()` blob onto Vera spans (the `lk.tts_metrics`
   pattern). It carries no PHI today but would attach whatever fields a future SDK version adds,
   sight unseen.
2. Do **not** attach `attribution.user_id` (§3.4) — it contributes nothing to cost.
3. Do **not** set `langfuse.observation.input` / `.output` on any span in this design, and disable
   `record_exception` / `set_status_on_exception` where a provider error could carry a payload.

Any step that would attach a value outside the allow-list returns to this design for a decision
rather than being added ad hoc.

## 9. Known debt this design touches but does not fix

`VertexLLMClient` bypassing `ResilientLLM` contradicts `vera_core/CLAUDE.md`'s rule that every
out-of-pipeline LLM call goes through that seam. The reason is real — the eval needs structured
output (`response_schema`) that `complete()` does not expose — but the rule now has a standing
exception that the doc does not record. This design instruments the client where it lives (D9).
Reconciling the two (extend `ResilientLLM` with a structured-output method, or amend the rule to
name the exception) is left as a follow-on so it does not ride along inside a cost change.

## 10. Testing

### 10.1 Automated gate (`just check`)

The pure/wiring split makes most assertions cheap — `usage_span_attributes` takes a hand-built
`STTMetrics`/`TTSMetrics`, no session, room, or event loop:

- Attribute shape per metric type: `usage_details` parses back to exactly
  `{"stt_audio_ms": ...}` / `{"tts_characters": ...}`; tokens appear only when non-zero;
- **Every `usage_details` value is an `int`** — asserted with `isinstance(v, int)` across a table
  of awkward `audio_duration` floats (`4.999999999999998`, a sub-millisecond remainder, `0.0`).
  A float here is truncated or dropped by Langfuse depending on route (§5.3), and both failures are
  silent; `27.64 → 27` in seconds would have been a 20% under-count per event.
  the model attributes track `metadata.model_name`.
- **`langfuse.observation.type == "generation"` on every cost-bearing span** — the §2.1 regression.
  Nothing else about the output looks wrong when this is missing; the cost column just goes blank.
- Zero-usage events produce no span (§5.1); a torn-down TTS request produces one *and* counts
  its characters, without reporting the meaningless `cancelled` flag (§5.2).
- `vera.usage.source` present on takeover spans, absent on cascade ones.
- PHI denylist via the existing `assert_no_phi_values(span, ...)` helper — extended to the eval
  span with a transcript-shaped string, since that is the one span whose inputs are PHI-dense.

**The load-bearing tests are the two parenting guarantees:**

1. **Worker capture (§3.5):** open a parent span, capture its context,
   `attach_usage_spans(..., parent_context=captured)`, emit the metrics event from a **separate
   `asyncio.Task`** (simulating the room-event callback path), then assert the span's parent is the
   captured span and the `trace_id` matches. A sibling test documents the failure it prevents — no
   captured context → a root span in its own trace.
2. **Cross-process join (§3.2):** round-trip a traceparent through the store, rebuild the remote
   parent, and assert a span opened under it carries the **same `trace_id`** as the originating
   worker span. Plus the degradation path: absent/corrupt key → a root span, no exception.

Span assertions use the existing `install_test_tracer_provider()` / `InMemorySpanExporter` harness
driven by a stub emitter, not a real `AgentSession`.

**Exporter-wrapper tests (§3.6)** are pure — a hand-built `ReadableSpan` in, an enriched span out:

- The invariant: `input + cached == prompt_tokens` from the source blob, so both the double-count
  and the under-count regressions fail loudly (§5.5).
- `cached` omitted when `prompt_cached_tokens == 0`; `usage_details` still present.
- A span **without** `lk.llm_metrics` passes through byte-identical.
- A malformed/renamed `lk.llm_metrics` exports the **original** span rather than dropping it —
  the SDK-upgrade degradation path (§3.6).
- The wrapper delegates `shutdown()` / `force_flush()` to the wrapped exporter, so no span is lost
  at process exit.

**Seeder tests** against a stubbed HTTP client: first run POSTs with no `modelId`; second run finds
the existing entry and threads its `modelId` back in (the specific behavior that avoids the
`(projectId, modelName)` rejection). A missing or unparseable rate exits non-zero with **no** HTTP
call issued.

### 10.2 What unit tests cannot prove

That Langfuse ingests, types, matches and prices these attributes. Every assertion above is about
what Vera emits.

### 10.3 Live verification (definition of done, D8)

1. `just langfuse-up`; set the rate env vars; `just langfuse-seed-prices`; confirm the entries
   under Settings → Models and read the coverage-gap log lines (§7.2).
2. `just up`, `just api`, `just worker`. No telephony needed — the browser-callee path suffices.
3. Place a test call; **join as supervisor and Intervene** so the dual-channel takeover STT runs;
   fire **hold-to-whisper** once; request a **summary**; let the call end so **post-call eval**
   runs. All eight sites exercised in one session.
4. In the trace: **every** generation carries a non-blank `$` — checked across all of them, not
   just the cascade's, since an uncovered model is the one failure that shows on some spans and
   not others. Takeover spans show both `vera.usage.source="rep"` and `="supervisor"`.
5. **The post-call eval and summary generations appear in the SAME trace as the call**, and that
   trace's total cost includes them. This is the whole point of §3.2 and the one thing no unit
   test covers — it also proves Langfuse accepts spans arriving after the trace's root span ended.
6. **Check the arithmetic by hand** — `characters × rate ≈ displayed cost`, and
   `audio_ms × rate ≈ displayed cost`. This is the only step that catches a unit mismatch between
   the instrumentation and the seeded rate (per-ms vs per-second vs per-minute is 1,000× or
   60,000×), which otherwise renders a perfectly plausible number. Also confirm a `vera.stt.usage`
   observation's stored usage is a **whole number of milliseconds** matching what Vera emitted —
   proving no truncation or drop occurred in ingestion (§5.3).
7. **Confirm the LLM cache split landed** (§2.6): on a multi-turn call, `llm_request` generations
   late in the call show a **non-zero `cached`** in `usage_details`, `input + cached` equals the
   `gen_ai.usage.input_tokens` the SDK set, and the cost is visibly lower than pricing the whole
   prompt at the input rate. A call with only one or two turns is **not** a valid check — implicit
   caching needs a repeated prefix, so a zero `cached` there proves nothing either way.

Per repo rules, the `/simplify` pass runs before commit and `just check` re-runs afterwards on the
exact tree being committed.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Late-arriving spans may not join an already-exported trace in Langfuse | §10.3 step 5 verifies directly; if it fails, fall back to session correlation plus a Metrics-API query for the per-call total (still avoids the #15109 UI path) |
| A per-second or per-minute rate entered into the per-millisecond price field | A 1,000×/60,000× error that renders as a plausible number. The env var and runbook name the unit (`..._PER_MS`), and §10.3 step 6 checks the arithmetic by hand — the only step that catches it |
| Usage values silently truncated or dropped for non-integers | Vera rounds to `int` at emit time (§5.3), so integrality never depends on the ingestion route; asserted in the unit tests (§10.1) |
| Redis trace-link key expiring before a sweeper-retried post-call job | 24h TTL (§3.2); on miss the eval still traces, just as its own trace — degraded, not broken |
| Uncovered model families render blank cost silently | Seeder coverage log (§7.2) + §10.3 step 4 checks every generation |
| Langfuse changes the implicit span→generation promotion rule | Type set explicitly (§3.1), so the promotion rule is never depended on |
| An SDK upgrade renames `lk.llm_metrics`, silently disabling the §3.6 correction | Falls back to exporting the original span (uncorrected, never dropped); §10.3 step 7 catches it on the next live verification. Regression-tested (§10.1) |
| Gemini model entry lacks a `cached` price, so cache hits price at $0 | Seeder owns explicit `vera-gemini-*` entries and treats a missing `cached` rate as fatal (§7.2) |
| Exporter wrapper drops or corrupts spans under load | Wrapper is additive, keyed on one attribute, falls through to the original on any exception, and delegates `shutdown`/`force_flush` (§10.1) |

## 12. Open follow-ons (not in this design's implementation plan)

- PHI-redacting `SpanProcessor` for the pre-existing SDK spans (`otel-spans-unredacted-pre-prod`)
  — **schedule before any production cutover**; this design makes traces more load-bearing, not less.
- Reconciling `VertexLLMClient` with the `ResilientLLM` rule (§9).
- Persisting per-call cost to Vera's database as a product-facing, queryable field.
- Pricing a token-billed STT/TTS provider, should one replace Deepgram/Cartesia — the
  `input_tokens`/`output_tokens` keys already flow through (§5), but the seeder would need
  matching price keys.
