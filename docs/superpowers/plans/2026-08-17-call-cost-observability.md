# Per-Call Provider Cost Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every billed provider surface in a Vera call — Deepgram STT, Cartesia TTS, and every Gemini/OpenAI call including the post-call eval — emit priceable usage into **one Langfuse trace per call**, and correct the LLM cost figure that today silently overstates spend by billing cache hits at the full input rate.

**Architecture:** One new module (`vera_core.observability.usage_spans`) turns LiveKit `metrics_collected` events into Langfuse **generation** observations carrying `langfuse.observation.usage_details` with integer usage keys. A second new module (`vera_core.observability.trace_link`) publishes the worker's W3C traceparent to Redis per call so the control plane's later spans join the same trace. The post-call eval gets its own generation spans at its single Vertex chokepoint, and an exporter wrapper corrects the SDK's `llm_request` spans in place. Vera holds no prices; an idempotent script seeds them into Langfuse.

**Tech Stack:** Python 3.12, `livekit-agents 1.6.7`, `opentelemetry-sdk`, `redis.asyncio`, `httpx>=0.28`, self-hosted `langfuse/langfuse:3`, pytest, `just`.

**Spec:** `docs/superpowers/specs/2026-08-17-call-cost-observability-design.md` — read §3.1 (the generation contract), §5.3 (why milliseconds), §5.5 (the LLM cache split), and §8 (PHI) before starting.

## Global Constraints

- **Per-task gate is lint + types + that task's own tests — NOT the full suite.** Each task runs `uv run ruff format --check . && uv run ruff check . && uv run mypy`, plus the specific test file that task wrote. The full `just check` runs **once, in Task 10**. This is a deliberate deviation from `vera-backend/CLAUDE.md`'s "run `just check` verbatim", authorized by the user for this plan: the whole pytest suite adds minutes per task with no signal for these changes. Task 10's full run is on the exact tree being pushed, so the CI contract still holds.
- **Environment setup, once before Task 1:** `cd vera-backend && uv sync --all-packages`. Plain `uv sync` leaves the venv without livekit/pytest and every test errors at conftest import. This worktree currently has an unsynced `.venv`.
- **PHI:** never attach transcript text, `SpeechEvent.alternatives[0].text`, extracted answer values, DTMF digits, or **the post-call eval's prompts/completions** to any span or log line. Only counts, durations, booleans, closed enums, fixed model names, W3C traceparents, and `call_trace_attributes` are permitted (spec §8).
- **Never copy** the SDK's `metrics.model_dump_json()` blob onto a Vera span (spec §8 prohibition 1). Never attach `attribution.user_id` (prohibition 2). Never set `langfuse.observation.input`/`.output` (prohibition 3).
- **Exception logging:** log `type(exc).__name__` only — never the exception repr or traceback (a provider error can embed the request payload). Never a bare `except` that would swallow `asyncio.CancelledError`.
- **Tracing must never break the call, the request, or the job:** every attach, span emit, and Redis touch is wrapped in `try/except Exception` → `logger.warning`.
- **Usage values are integers.** Langfuse stores `usage_details` as `Map(String, UInt64)`; floats are truncated (OTel route) or dropped (SDK route). Round in Vera, never rely on ingestion (spec §5.3).
- **Style:** PEP 695 type params (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`. `asyncio` only; never `import anyio`.
- **`Metadata` is NOT exported from `livekit.agents.metrics`** — import it from `livekit.agents.metrics.base`. (The superseded 2026-07-28 plan got this wrong; its `from livekit.agents.metrics import Metadata` would raise `ImportError`.)
- All paths below are relative to `vera-backend/` unless stated otherwise; the two doc paths in Task 10 are relative to the repo root.

## File Structure

| File | Responsibility |
|---|---|
| `packages/vera_core/src/vera_core/observability/usage_spans.py` | **New.** Pure attribute builder + the `metrics_collected` listener. The only file that knows the Langfuse generation/usage contract. |
| `packages/vera_core/src/vera_core/observability/trace_link.py` | **New.** W3C traceparent capture/adoption + the Redis per-call handoff. The only file that knows how a call's trace crosses processes. |
| `packages/vera_core/src/vera_core/observability/llm_usage_export.py` | **New.** The exporter wrapper correcting `llm_request` spans' cached-token split. |
| `packages/vera_core/src/vera_core/observability/__init__.py` | Modify — re-export the new public functions. |
| `packages/vera_core/src/vera_core/observability/otel.py` | Modify — wrap the OTLP exporter. |
| `apps/agent_worker/src/agent_worker/cascade.py` | Modify `build_session` — bind `stt`/`tts` to locals, attach listeners. |
| `apps/agent_worker/src/agent_worker/main.py` | Modify — capture the entrypoint context, publish the trace link, pass context to `build_session`, replace the takeover `stt_factory` lambda. |
| `apps/agent_worker/src/agent_worker/takeover_transcript.py` | Modify — widen `stt_factory` to receive `SpeakerAttribution`. |
| `packages/vera_core/src/vera_core/stt.py` | Modify `_adapter()` — attach one listener to the `FallbackAdapter`. |
| `apps/control_plane/src/control_plane/api/v1/coaching.py` | Modify — wrap the whisper `transcribe()` in a span parented at the call's trace. |
| `apps/control_plane/src/control_plane/llm.py` | Modify `VertexLLMClient` — emit a generation per Vertex call. |
| `apps/control_plane/src/control_plane/post_call_consumer.py` | Modify — resolve the remote parent and run the job under it. |
| `apps/control_plane/src/control_plane/call_summary.py` | Modify — run the summary LLM call under the call's trace. |
| `apps/control_plane/src/control_plane/deps.py` / `main.py` | Modify — build and expose a `TraceLinkStore`. |
| `scripts/seed_langfuse_prices.py` | **New.** Idempotent Langfuse model-price seeder. |
| `justfile` | Modify — add `langfuse-seed-prices`. |
| `adr/devops-todo.md` | Modify — add the seeding row. |
| `docs/superpowers/specs/2026-08-17-langfuse-price-entry-runbook.md` | **New.** Manual price-entry fallback (repo root). |

**Tests:**

| File | Covers |
|---|---|
| `tests/unit/observability/test_usage_spans.py` | **New.** Tasks 1-2: attribute shape, integer units, zero-usage skip, cancelled TTS, PHI, trace parenting. |
| `tests/unit/observability/test_trace_link.py` | **New.** Task 3: traceparent round-trip, cross-process join, degradation. |
| `tests/unit/observability/test_llm_usage_export.py` | **New.** Task 9: cached-token split, passthrough, malformed-blob fallback. |
| `tests/unit/observability/test_seed_langfuse_prices.py` | **New.** Task 10: seeder idempotency, refuse-to-seed-zero. |
| `apps/agent_worker/tests/unit/test_cascade.py` | Extend — Task 4 cascade wiring. |
| `tests/unit/agent_worker/test_takeover_transcript.py` | Extend — Task 5 per-track factory. |
| `tests/unit/control_plane/test_post_call_llm_spans.py` | **New.** Task 7: eval generation shape + PHI. |

---

### Task 1: The pure attribute builder

Turns one STT/TTS metrics event into the exact generation attributes Langfuse prices. No OTel, no I/O — so the part Langfuse contracts on is testable in isolation.

**Files:**
- Create: `packages/vera_core/src/vera_core/observability/usage_spans.py`
- Test: `tests/unit/observability/test_usage_spans.py`

**Interfaces:**
- Consumes: `livekit.agents.metrics.STTMetrics` / `TTSMetrics`; `livekit.agents.metrics.base.Metadata`.
- Produces:
  - `usage_span_attributes(metrics: Any) -> dict[str, str | int | float | bool] | None`
  - Constants `SPAN_STT_USAGE = "vera.stt.usage"`, `SPAN_TTS_USAGE = "vera.tts.usage"`, `OBSERVATION_TYPE_ATTR`, `OBSERVATION_MODEL_ATTR`, `USAGE_DETAILS_ATTR`, `GENERATION`, `STT_AUDIO_MS = "stt_audio_ms"`, `TTS_CHARACTERS = "tts_characters"`, `USAGE_INPUT = "input"`, `USAGE_OUTPUT = "output"`
  - Type alias `type UsageAttributes = dict[str, str | int | float | bool]`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/test_usage_spans.py`:

```python
"""Usage-generation attribute shape (design §3.1, §5) — the Langfuse contract, tested pure."""

import json
from typing import Any

from livekit.agents.metrics import STTMetrics, TTSMetrics
from livekit.agents.metrics.base import Metadata

from vera_core.observability.usage_spans import (
    GENERATION,
    OBSERVATION_MODEL_ATTR,
    OBSERVATION_TYPE_ATTR,
    STT_AUDIO_MS,
    TTS_CHARACTERS,
    USAGE_DETAILS_ATTR,
    usage_span_attributes,
)


def _stt(**over: Any) -> STTMetrics:
    base: dict[str, Any] = {
        "request_id": "req-1",
        "timestamp": 1.0,
        "duration": 0.0,
        "label": "deepgram.STTv2",
        "audio_duration": 27.64,
        "streamed": True,
        "metadata": Metadata(model_name="flux-general-en", model_provider="Deepgram"),
    }
    return STTMetrics(**{**base, **over})


def _tts(**over: Any) -> TTSMetrics:
    base: dict[str, Any] = {
        "request_id": "req-2",
        "timestamp": 1.0,
        "ttfb": 0.134,
        "duration": 4.72,
        "audio_duration": 27.64,
        "cancelled": False,
        "characters_count": 465,
        "streamed": True,
        "metadata": Metadata(model_name="sonic-3.5-2026-05-04", model_provider="Cartesia"),
    }
    return TTSMetrics(**{**base, **over})


def _usage(attrs: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(attrs[USAGE_DETAILS_ATTR])
    return parsed


class TestGenerationTyping:
    """Langfuse prices ONLY generation/embedding observations. A plain span ingests
    cleanly and renders blank cost — indistinguishable from broken instrumentation.
    This is the error that sank the superseded 2026-07-28 design."""

    def test_stt_is_typed_a_generation(self) -> None:
        attrs = usage_span_attributes(_stt())
        assert attrs is not None
        assert attrs[OBSERVATION_TYPE_ATTR] == GENERATION

    def test_tts_is_typed_a_generation(self) -> None:
        attrs = usage_span_attributes(_tts())
        assert attrs is not None
        assert attrs[OBSERVATION_TYPE_ATTR] == GENERATION


class TestIntegerUsage:
    """Langfuse stores usage_details as Map(String, UInt64): a float is truncated on
    the OTel route and DROPPED on the SDK route (design §5.3). Vera rounds so the
    value is an integer by construction, whichever route it takes."""

    def test_audio_is_whole_milliseconds(self) -> None:
        attrs = usage_span_attributes(_stt(audio_duration=27.64))
        assert attrs is not None
        assert _usage(attrs) == {STT_AUDIO_MS: 27640}

    def test_every_usage_value_is_an_int(self) -> None:
        for duration in (27.64, 4.999999999999998, 0.0005, 300.0):
            attrs = usage_span_attributes(_stt(audio_duration=duration))
            if attrs is None:
                continue
            for key, value in _usage(attrs).items():
                assert isinstance(value, int), f"{key}={value!r} is not an int"

    def test_a_five_second_chunk_does_not_lose_a_second(self) -> None:
        # Float summation of frame durations lands just under as often as just over.
        # In seconds this truncated to 4 — a 20% under-count, on most events of every
        # call, silently. In milliseconds the same value loses 0.02%.
        attrs = usage_span_attributes(_stt(audio_duration=4.999999999999998))
        assert attrs is not None
        assert _usage(attrs)[STT_AUDIO_MS] == 5000

    def test_characters_pass_through_as_the_integer_they_already_are(self) -> None:
        attrs = usage_span_attributes(_tts())
        assert attrs is not None
        assert _usage(attrs) == {TTS_CHARACTERS: 465}


class TestModelAttribution:
    def test_model_name_is_set_for_langfuse_and_otel(self) -> None:
        # Langfuse regex-matches its price entry against the model; langfuse.* wins on
        # precedence, gen_ai.* is the OTel semantic convention. Both are set.
        attrs = usage_span_attributes(_stt())
        assert attrs is not None
        assert attrs[OBSERVATION_MODEL_ATTR] == "flux-general-en"
        assert attrs["gen_ai.request.model"] == "flux-general-en"
        assert attrs["gen_ai.provider.name"] == "Deepgram"

    def test_missing_metadata_omits_model_attributes(self) -> None:
        attrs = usage_span_attributes(_stt(metadata=None))
        assert attrs is not None
        assert OBSERVATION_MODEL_ATTR not in attrs
        assert _usage(attrs) == {STT_AUDIO_MS: 27640}


class TestZeroAndCancelled:
    def test_connection_acquired_event_yields_no_span(self) -> None:
        # stt.py:439 _report_connection_acquired emits a real STTMetrics with zero usage
        # purely to report websocket connect timing (design §5.1). A span for it would
        # add a $0 noise generation per connect.
        assert usage_span_attributes(_stt(audio_duration=0.0, request_id="")) is None

    def test_sub_millisecond_audio_yields_no_span(self) -> None:
        assert usage_span_attributes(_stt(audio_duration=0.0004)) is None

    def test_empty_synthesis_yields_no_span(self) -> None:
        assert usage_span_attributes(_tts(characters_count=0)) is None

    def test_cancelled_tts_still_counts_its_characters(self) -> None:
        # Barge-in: those characters already went to Cartesia and are billed (design §5.2).
        attrs = usage_span_attributes(_tts(cancelled=True))
        assert attrs is not None
        assert _usage(attrs) == {TTS_CHARACTERS: 465}
        assert attrs["vera.usage.cancelled"] is True

    def test_tts_audio_seconds_is_operational_not_billed(self) -> None:
        attrs = usage_span_attributes(_tts())
        assert attrs is not None
        assert attrs["vera.usage.audio_seconds"] == 27.64
        assert "audio_duration" not in _usage(attrs)


class TestTokenBilledProviders:
    def test_tokens_fold_in_when_non_zero(self) -> None:
        attrs = usage_span_attributes(_stt(input_tokens=120, output_tokens=8))
        assert attrs is not None
        assert _usage(attrs) == {STT_AUDIO_MS: 27640, "input": 120, "output": 8}

    def test_zero_tokens_are_omitted_not_sent_as_zero(self) -> None:
        # A zero-valued key would demand a Langfuse price entry for a unit nobody bills.
        assert "input" not in _usage(usage_span_attributes(_stt()) or {})


class TestDefensiveHandling:
    def test_unknown_metrics_type_yields_none(self) -> None:
        assert usage_span_attributes(object()) is None

    def test_vad_metrics_are_not_priced(self) -> None:
        assert usage_span_attributes(None) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'vera_core.observability.usage_spans'`

- [ ] **Step 3: Write the module**

Create `packages/vera_core/src/vera_core/observability/usage_spans.py`:

```python
"""Per-request STT/TTS usage as Langfuse-priceable generations.

LiveKit reports STT usage ONLY through the OTel *Metrics* API (a no-op meter here,
since `otel.py` installs a TracerProvider and nothing else) and Langfuse ingests
traces only — so STT usage reaches Langfuse nowhere. TTS does get a span, but its
usage rides a custom `lk.tts_metrics` bag Langfuse's cost engine does not read.

Both are fixed the same way: one short Vera-owned observation per
`metrics_collected` event, typed `generation` and carrying
`langfuse.observation.usage_details`. Langfuse parses that attribute with ARBITRARY
usage keys and matches each against a model price entry, so non-token billing units
price exactly like tokens. Vera holds no rates — see `scripts/seed_langfuse_prices.py`.

Two contracts this module must not break:

- **Only `generation` and `embedding` observations carry cost.** A plain span ingests
  cleanly and renders BLANK cost, which looks identical to broken instrumentation.
- **Usage values must be integers.** Langfuse stores them as `Map(String, UInt64)`;
  a float is truncated on the OTel route and dropped outright on the SDK route. Audio
  is therefore reported in whole milliseconds, rounded here rather than in ingestion.

PHI: neither STTMetrics nor TTSMetrics carries any text field (`characters_count` is
`len(input_text)`, a length). Every attribute below is a count, duration, boolean,
closed enum, or a model name Vera itself passed in. Never add the SDK's
`metrics.model_dump_json()` blob here — it carries no PHI today but would attach
whatever fields a future SDK version adds, sight unseen.
"""

import json
import logging
from typing import Any

from livekit.agents.metrics import STTMetrics, TTSMetrics

logger = logging.getLogger("vera.observability")

SPAN_STT_USAGE = "vera.stt.usage"
SPAN_TTS_USAGE = "vera.tts.usage"

# Langfuse's own namespace, which takes precedence over the gen_ai.* conventions.
OBSERVATION_TYPE_ATTR = "langfuse.observation.type"
OBSERVATION_MODEL_ATTR = "langfuse.observation.model.name"
USAGE_DETAILS_ATTR = "langfuse.observation.usage_details"

# Set explicitly rather than relying on Langfuse's implicit "a span with a model
# attribute becomes a generation" promotion — an inference rule that has changed once
# already, and cost silently disappears when it changes again.
GENERATION = "generation"

# Usage keys. These strings are a contract with the seeded Langfuse price entries —
# changing one here without changing it there silently zeroes cost.
STT_AUDIO_MS = "stt_audio_ms"
TTS_CHARACTERS = "tts_characters"
USAGE_INPUT = "input"
USAGE_OUTPUT = "output"

type UsageAttributes = dict[str, str | int | float | bool]


def _billable_tokens(metrics: STTMetrics | TTSMetrics) -> dict[str, int]:
    """Token counts, included only when non-zero: Deepgram and Cartesia both report 0,
    and a zero-valued key would demand a price entry for a unit nobody bills. The key
    names match the LLM vocabulary so one model entry can price both."""
    tokens: dict[str, int] = {}
    if metrics.input_tokens:
        tokens[USAGE_INPUT] = metrics.input_tokens
    if metrics.output_tokens:
        tokens[USAGE_OUTPUT] = metrics.output_tokens
    return tokens


def usage_span_attributes(metrics: Any) -> UsageAttributes | None:
    """Generation attributes for one metrics event, or None when nothing is billable.

    None covers three real cases: `_report_connection_acquired`'s zero-usage
    connection-timing event (`stt.py:439`), audio too short to round to a
    millisecond, and any metrics type that is not STT/TTS (VAD/EOU cost nothing).
    """
    usage: dict[str, int]
    attrs: UsageAttributes
    if isinstance(metrics, TTSMetrics):
        usage = _billable_tokens(metrics)
        if metrics.characters_count:
            usage[TTS_CHARACTERS] = metrics.characters_count
        if not usage:
            return None
        attrs = {
            "vera.usage.streamed": metrics.streamed,
            "vera.usage.cancelled": metrics.cancelled,
            # Operational only — Cartesia bills characters, not output duration.
            "vera.usage.audio_seconds": metrics.audio_duration,
        }
    elif isinstance(metrics, STTMetrics):
        usage = _billable_tokens(metrics)
        if audio_ms := round(metrics.audio_duration * 1000):
            usage[STT_AUDIO_MS] = audio_ms
        if not usage:
            return None
        attrs = {"vera.usage.streamed": metrics.streamed}
    else:
        return None

    attrs[OBSERVATION_TYPE_ATTR] = GENERATION
    attrs[USAGE_DETAILS_ATTR] = json.dumps(usage)
    if (meta := metrics.metadata) is not None:
        if meta.model_name:
            attrs[OBSERVATION_MODEL_ATTR] = meta.model_name
            attrs["gen_ai.request.model"] = meta.model_name
        if meta.model_provider:
            attrs["gen_ai.provider.name"] = meta.model_provider
    return attrs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS. If ruff reformats, run `uv run ruff format .` and re-run.

- [ ] **Step 6: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/observability/usage_spans.py \
        vera-backend/tests/unit/observability/test_usage_spans.py
git commit -m "feat(otel): build Langfuse-priceable generation attributes from STT/TTS metrics"
```

---

### Task 2: The listener and trace parenting

Registers the `metrics_collected` listener and emits the generation. The parenting test here is load-bearing — see spec §3.5.

**Files:**
- Modify: `packages/vera_core/src/vera_core/observability/usage_spans.py`
- Modify: `packages/vera_core/src/vera_core/observability/__init__.py`
- Test: `tests/unit/observability/test_usage_spans.py` (append)

**Interfaces:**
- Consumes: `usage_span_attributes` (Task 1); `call_trace_attributes` from `vera_core.observability.correlation`.
- Produces: `attach_usage_spans(emitter: Any, *, parent_context: Context | None = None, room_name: str | None = None, source: str | None = None) -> None`. Tasks 4, 5 and 6 all call exactly this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/observability/test_usage_spans.py`:

```python
import asyncio

import pytest
from livekit import rtc
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import assert_no_phi_values
from vera_core.observability.usage_spans import (
    SPAN_STT_USAGE,
    SPAN_TTS_USAGE,
    attach_usage_spans,
)


class _FakeEmitter(rtc.EventEmitter[str]):
    """Stands in for deepgram.STTv2 / cartesia.TTS / stt.FallbackAdapter — all of
    which are rtc.EventEmitters that emit 'metrics_collected'."""


def _only(exporter: InMemorySpanExporter, name: str) -> Any:
    return next(s for s in exporter.get_finished_spans() if s.name == name)


class TestListener:
    def test_stt_event_emits_a_named_generation(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt())
        assert _only(otel_spans, SPAN_STT_USAGE) is not None

    def test_tts_event_emits_a_named_generation(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _tts())
        assert _only(otel_spans, SPAN_TTS_USAGE) is not None

    def test_zero_usage_event_emits_nothing(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt(audio_duration=0.0, request_id=""))
        assert otel_spans.get_finished_spans() == ()

    def test_room_name_adds_call_correlation(self, otel_spans: Any) -> None:
        tenant = "00000000-0000-0000-0000-0000000000aa"
        call = "00000000-0000-0000-0000-0000000000bb"
        room = f"call--{tenant}--{call}"
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, room_name=room)
        emitter.emit("metrics_collected", _stt())
        span = _only(otel_spans, SPAN_STT_USAGE)
        assert span.attributes["langfuse.session.id"] == room
        assert span.attributes["vera.call_id"] == call

    def test_source_is_set_only_when_given(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, source="supervisor")
        emitter.emit("metrics_collected", _stt())
        assert _only(otel_spans, SPAN_STT_USAGE).attributes["vera.usage.source"] == "supervisor"

    def test_cascade_span_has_no_source(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt())
        assert "vera.usage.source" not in _only(otel_spans, SPAN_STT_USAGE).attributes

    def test_no_phi_reaches_the_span(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _tts())
        # Substring check: an attribute merely EMBEDDING a transcript fails too.
        assert_no_phi_values(_only(otel_spans, SPAN_TTS_USAGE), "Jane Doe", "member id 1234")

    def test_a_broken_event_never_propagates_to_the_caller(self, otel_spans: Any) -> None:
        # A tracing failure must never break the call (design §6).
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", None)  # must not raise
        assert otel_spans.get_finished_spans() == ()


class TestTraceParenting:
    """Design §3.5: the takeover STT is reached via a room-event callback whose task
    does NOT carry job_entrypoint's context. Without an explicitly captured parent,
    usage spans become NEW TRACE ROOTS — they fall out of the call's trace and never
    sum into its cost, and nothing else about the output looks wrong."""

    @pytest.mark.asyncio
    async def test_captured_context_survives_a_foreign_task(self, otel_spans: Any) -> None:
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as parent:
            captured = otel_context.get_current()
            parent_ctx = parent.get_span_context()
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, parent_context=captured)

        async def foreign_task() -> None:
            # A task created OUTSIDE the parent span's context, like LiveKit's room
            # event dispatch task.
            emitter.emit("metrics_collected", _stt())

        await asyncio.create_task(foreign_task())

        span = _only(otel_spans, SPAN_STT_USAGE)
        assert span.parent is not None
        assert span.parent.span_id == parent_ctx.span_id
        assert span.context.trace_id == parent_ctx.trace_id

    @pytest.mark.asyncio
    async def test_without_a_captured_context_the_span_is_orphaned(self, otel_spans: Any) -> None:
        """Documents exactly what §3.5 prevents, so the guarantee above is meaningful."""
        tracer = trace.get_tracer("test")
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)  # no parent_context — the bug

        ready = asyncio.Event()
        done = asyncio.Event()

        async def dispatch_task() -> None:
            await ready.wait()
            emitter.emit("metrics_collected", _stt())
            done.set()

        task = asyncio.create_task(dispatch_task())
        with tracer.start_as_current_span("job_entrypoint"):
            ready.set()
            await done.wait()
        await task

        # No parent: a root span in its own trace, which is the failure mode.
        assert _only(otel_spans, SPAN_STT_USAGE).parent is None

    def test_ambient_context_is_used_when_no_parent_is_captured(self, otel_spans: Any) -> None:
        # The coaching-whisper path relies on this: its parent span is per-request, so
        # it CANNOT be captured at attach time (Task 6).
        tracer = trace.get_tracer("test")
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        with tracer.start_as_current_span("vera.coaching.whisper") as parent:
            emitter.emit("metrics_collected", _stt())
            expected = parent.get_span_context()
        span = _only(otel_spans, SPAN_STT_USAGE)
        assert span.parent is not None
        assert span.parent.span_id == expected.span_id
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v -k "Listener or Parenting"`
Expected: FAIL with `ImportError: cannot import name 'attach_usage_spans'`

- [ ] **Step 3: Append the listener to the module**

Add these imports to the top of `usage_spans.py`:

```python
from opentelemetry import trace
from opentelemetry.context import Context

from vera_core.observability.correlation import call_trace_attributes
```

Add after the constants:

```python
_tracer = trace.get_tracer("vera.observability.usage")
```

Append to the end of `packages/vera_core/src/vera_core/observability/usage_spans.py`:

```python
def _emit_usage_span(
    metrics: Any,
    *,
    parent_context: Context | None,
    room_name: str | None,
    source: str | None,
) -> None:
    attrs = usage_span_attributes(metrics)
    if attrs is None:
        return
    if room_name is not None:
        attrs.update(call_trace_attributes(room_name))
    if source is not None:
        attrs["vera.usage.source"] = source
    name = SPAN_TTS_USAGE if isinstance(metrics, TTSMetrics) else SPAN_STT_USAGE
    # A point-in-time observation: every attribute is known up front, so open and close
    # immediately. `context=None` means "use the ambient context", which is what the
    # per-request coaching path needs; the worker paths pass an explicitly captured one.
    _tracer.start_span(name, context=parent_context, attributes=attrs).end()


def attach_usage_spans(
    emitter: Any,
    *,
    parent_context: Context | None = None,
    room_name: str | None = None,
    source: str | None = None,
) -> None:
    """Emit one usage generation per billable `metrics_collected` event from *emitter*.

    *emitter* is any livekit `rtc.EventEmitter` that emits `metrics_collected` — a
    plugin STT/TTS instance or an STT `FallbackAdapter` (which re-emits its inner
    metrics verbatim, so the model name stays the real provider's).

    *parent_context* MUST be captured where the intended parent span is genuinely
    ambient. In the agent worker that is the job entrypoint: the takeover STT is also
    reached from a room-event callback whose task does not carry the entrypoint's
    context, so capturing at emit time would silently produce a new trace root. Leave
    it None only when the intended parent is per-invocation (the coaching-whisper
    request span), where ambient context is the correct answer.

    Listens on the component-level emitter, NOT `session.on("metrics_collected")` —
    the latter is deprecated in livekit-agents 1.6.7 and warns per registration.
    """

    def _on_metrics(metrics: Any) -> None:
        try:
            _emit_usage_span(
                metrics, parent_context=parent_context, room_name=room_name, source=source
            )
        except Exception as exc:
            # Never let a tracing failure reach the call, the transcript, or the request.
            # Type name only: a provider error can embed the request payload.
            logger.warning("usage span emit failed: %s", type(exc).__name__)

    try:
        emitter.on("metrics_collected", _on_metrics)
    except Exception as exc:
        logger.warning("usage span attach failed: %s", type(exc).__name__)
```

- [ ] **Step 4: Export the public functions**

Replace `packages/vera_core/src/vera_core/observability/__init__.py` with:

```python
from .correlation import call_trace_attributes, parse_room_name, room_name_for_call
from .otel import configure_observability
from .usage_spans import attach_usage_spans, usage_span_attributes

__all__ = [
    "attach_usage_spans",
    "call_trace_attributes",
    "configure_observability",
    "parse_room_name",
    "room_name_for_call",
    "usage_span_attributes",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/observability/ \
        vera-backend/tests/unit/observability/test_usage_spans.py
git commit -m "feat(otel): emit per-turn STT/TTS usage generations with an explicit trace parent"
```

---

### Task 3: The cross-process trace link

Publishes the worker's traceparent per call so the control plane's later spans join the call's trace. Without this, post-call eval and summary cost never sums into the call total — and Langfuse's session rollup cannot substitute (langfuse#15109).

**Files:**
- Create: `packages/vera_core/src/vera_core/observability/trace_link.py`
- Modify: `packages/vera_core/src/vera_core/observability/__init__.py`
- Test: `tests/unit/observability/test_trace_link.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis`.
- Produces:
  - `current_traceparent() -> str | None`
  - `remote_parent(traceparent: str | None) -> Context | None`
  - `trace_link_key(room_name: str) -> str`
  - `class TraceLinkStore` with `async def publish(self, room_name: str, traceparent: str) -> None` and `async def resolve(self, room_name: str) -> Context | None`
  - `TRACE_LINK_TTL_SECONDS: int`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/test_trace_link.py`:

```python
"""Cross-process trace join (design §3.2). The guarantee under test is that a span
opened in the control plane lands in the SAME TRACE as the worker's call span —
Langfuse rolls cost up reliably per trace, and its session rollup renders $0.00 for
model-calculated cost (langfuse#15109), so this is what makes a per-call total real."""

from typing import Any

import pytest
from opentelemetry import trace

from vera_core.observability.trace_link import (
    TraceLinkStore,
    current_traceparent,
    remote_parent,
    trace_link_key,
)

_ROOM = "call--00000000-0000-0000-0000-0000000000aa--00000000-0000-0000-0000-0000000000bb"


class _FakeRedis:
    """Minimal get/set stand-in; `fails` makes every call raise, standing in for a
    Redis outage."""

    def __init__(self, *, fails: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._fails = fails

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self._fails:
            raise ConnectionError("redis down")
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> bytes | None:
        if self._fails:
            raise ConnectionError("redis down")
        value = self.values.get(key)
        return value.encode() if value is not None else None


class TestKey:
    def test_key_follows_the_per_call_convention(self) -> None:
        # Matches vera:call-plan:<room> / vera:summary:<room> / vera:call-events:<room>.
        assert trace_link_key(_ROOM) == f"vera:trace:{_ROOM}"


class TestCapture:
    def test_traceparent_is_captured_from_the_ambient_span(self, otel_spans: Any) -> None:
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as span:
            traceparent = current_traceparent()
            expected = span.get_span_context()
        assert traceparent is not None
        assert f"{expected.trace_id:032x}" in traceparent


class TestAdoption:
    def test_a_span_under_the_remote_parent_joins_the_original_trace(
        self, otel_spans: Any
    ) -> None:
        """THE load-bearing assertion: same trace_id across a process boundary."""
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_ctx = worker_span.get_span_context()

        parent = remote_parent(traceparent)
        assert parent is not None
        with tracer.start_as_current_span("vera.post_call.eval", context=parent) as later:
            assert later.get_span_context().trace_id == worker_ctx.trace_id

    def test_absent_traceparent_degrades_to_a_root_span(self) -> None:
        # Graceful degradation: the eval still traces, just as its own trace.
        assert remote_parent(None) is None
        assert remote_parent("") is None

    def test_malformed_traceparent_degrades_to_a_root_span(self) -> None:
        assert remote_parent("not-a-traceparent") is None
        assert remote_parent("00-0000000000000000000000000000000-0000000000000000-01") is None


class TestStore:
    @pytest.mark.asyncio
    async def test_publish_then_resolve_round_trips(self, otel_spans: Any) -> None:
        redis = _FakeRedis()
        store = TraceLinkStore(redis)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_ctx = worker_span.get_span_context()
        assert traceparent is not None
        await store.publish(_ROOM, traceparent)

        parent = await store.resolve(_ROOM)
        assert parent is not None
        with tracer.start_as_current_span("later", context=parent) as later:
            assert later.get_span_context().trace_id == worker_ctx.trace_id

    @pytest.mark.asyncio
    async def test_publish_sets_a_ttl(self) -> None:
        # Sized for the longest post-call window: the sweeper can re-drive a stranded
        # job minutes after call.ended, long after the call itself is over.
        redis = _FakeRedis()
        await TraceLinkStore(redis).publish(_ROOM, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert redis.ttls[trace_link_key(_ROOM)] >= 3600

    @pytest.mark.asyncio
    async def test_a_missing_key_resolves_to_none(self) -> None:
        assert await TraceLinkStore(_FakeRedis()).resolve(_ROOM) is None

    @pytest.mark.asyncio
    async def test_a_redis_outage_never_raises(self) -> None:
        # Tracing must never break a call or an API request (design §6).
        store = TraceLinkStore(_FakeRedis(fails=True))
        await store.publish(_ROOM, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert await store.resolve(_ROOM) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_trace_link.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'vera_core.observability.trace_link'`

- [ ] **Step 3: Write the module**

Create `packages/vera_core/src/vera_core/observability/trace_link.py`:

```python
"""Cross-process trace join for one call.

Langfuse rolls cost up reliably per TRACE; its SESSION rollup renders $0.00 when cost
is model-calculated rather than caller-ingested (langfuse#15109), which is exactly
Vera's case since prices live in Langfuse. So a per-call total is only real if every
span belonging to the call shares ONE trace id — including the ones the control plane
emits minutes later (post-call eval, summary) or mid-call from a browser request
(coaching whisper).

The worker cannot derive that id: LiveKit mints the `job_entrypoint` span before
Vera's code runs, and every auto-instrumented STT/LLM/TTS span hangs beneath it. A
deterministic id computed from the room name would create a SECOND trace alongside
it. So the id is propagated, not derived: the worker publishes its W3C traceparent
once per call, keyed by room name, and the control plane adopts it as a remote parent.

Every operation is best-effort. A missing or unusable link degrades to a root span —
the work still traces, it just forms its own trace — and never raises into a call or
an HTTP request.

PHI: a traceparent is random hex identifiers only.
"""

import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger("vera.observability")

# Sized for the longest post-call window, not the call: post-call eval normally runs
# seconds after call.ended, but the pipeline sweeper can re-drive a stranded job much
# later. One small key per call is negligible.
TRACE_LINK_TTL_SECONDS = 24 * 60 * 60

_PROPAGATOR = TraceContextTextMapPropagator()
_TRACEPARENT = "traceparent"


def trace_link_key(room_name: str) -> str:
    """Per-call Redis key, following the `vera:<thing>:<room>` convention."""
    return f"vera:trace:{room_name}"


def current_traceparent() -> str | None:
    """The ambient OTel context as a W3C traceparent, or None when tracing is a no-op.

    MUST be called where the intended parent span is genuinely ambient — in the agent
    worker that is the job entrypoint, where LiveKit's `job_entrypoint` span is active.
    """
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get(_TRACEPARENT)


def remote_parent(traceparent: str | None) -> Context | None:
    """A Context parented at *traceparent*, or None when it is absent or unusable.

    None is the graceful-degradation signal meaning "open a root span instead".
    """
    if not traceparent:
        return None
    ctx = _PROPAGATOR.extract({_TRACEPARENT: traceparent})
    if not trace.get_current_span(ctx).get_span_context().is_valid:
        return None
    return ctx


class TraceLinkStore:
    """Publishes and resolves a call's traceparent over Redis.

    Both methods swallow Redis failures: an observability outage must never affect a
    call or an API request.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, room_name: str, traceparent: str) -> None:
        try:
            await self._redis.set(
                trace_link_key(room_name), traceparent, ex=TRACE_LINK_TTL_SECONDS
            )
        except Exception as exc:
            logger.warning("trace link publish failed: %s", type(exc).__name__)

    async def resolve(self, room_name: str) -> Context | None:
        try:
            raw = await self._redis.get(trace_link_key(room_name))
        except Exception as exc:
            logger.warning("trace link resolve failed: %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        return remote_parent(raw.decode() if isinstance(raw, bytes) else str(raw))
```

- [ ] **Step 4: Export the new names**

Replace `packages/vera_core/src/vera_core/observability/__init__.py` with:

```python
from .correlation import call_trace_attributes, parse_room_name, room_name_for_call
from .otel import configure_observability
from .trace_link import TraceLinkStore, current_traceparent, remote_parent
from .usage_spans import attach_usage_spans, usage_span_attributes

__all__ = [
    "TraceLinkStore",
    "attach_usage_spans",
    "call_trace_attributes",
    "configure_observability",
    "current_traceparent",
    "parse_room_name",
    "remote_parent",
    "room_name_for_call",
    "usage_span_attributes",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_trace_link.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/observability/ \
        vera-backend/tests/unit/observability/test_trace_link.py
git commit -m "feat(otel): carry a call's trace across processes via a Redis traceparent link"
```

---

### Task 4: Wire the cascade STT and TTS, capture the entrypoint context, publish the trace link

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/cascade.py:127-153`
- Modify: `apps/agent_worker/src/agent_worker/main.py:387` (after the `call_trace_attributes` line) and `:479-485` (the `build_session` call)
- Test: `apps/agent_worker/tests/unit/test_cascade.py` (extend)

**Interfaces:**
- Consumes: `attach_usage_spans` (Task 2); `TraceLinkStore`, `current_traceparent` (Task 3).
- Produces: `build_session(..., room_name: str | None = None, parent_context: Context | None = None)`; the `usage_parent_ctx` local in `main.py`'s entrypoint, which Task 5 reuses.

- [ ] **Step 1: Write the failing test**

Append to `apps/agent_worker/tests/unit/test_cascade.py`:

```python
class TestUsageSpanWiring:
    """Without these listeners, Deepgram and Cartesia spend is invisible in Langfuse:
    LiveKit reports STT usage only to the OTel Metrics API (a no-op meter here) and
    TTS usage only in a custom attribute bag Langfuse's cost engine does not read."""

    def test_cascade_tts_emits_a_usage_generation(self, otel_spans: Any) -> None:
        from livekit.agents.metrics import TTSMetrics
        from livekit.agents.metrics.base import Metadata

        from vera_core.observability.usage_spans import SPAN_TTS_USAGE

        session = build_session(vad=object(), default_model="gemini-2.5-flash")
        session.tts.emit(
            "metrics_collected",
            TTSMetrics(
                request_id="r",
                timestamp=1.0,
                ttfb=0.1,
                duration=1.0,
                audio_duration=2.0,
                cancelled=False,
                characters_count=42,
                streamed=True,
                metadata=Metadata(model_name="sonic-3.5-2026-05-04", model_provider="Cartesia"),
            ),
        )
        span = next(s for s in otel_spans.get_finished_spans() if s.name == SPAN_TTS_USAGE)
        assert span.attributes["gen_ai.request.model"] == "sonic-3.5-2026-05-04"

    def test_cascade_stt_emits_a_usage_generation(self, otel_spans: Any) -> None:
        from livekit.agents.metrics import STTMetrics
        from livekit.agents.metrics.base import Metadata

        from vera_core.observability.usage_spans import SPAN_STT_USAGE

        session = build_session(vad=object(), default_model="gemini-2.5-flash")
        session.stt.emit(
            "metrics_collected",
            STTMetrics(
                request_id="r",
                timestamp=1.0,
                duration=0.0,
                label="deepgram.STTv2",
                audio_duration=5.0,
                streamed=True,
                metadata=Metadata(model_name="flux-general-en", model_provider="Deepgram"),
            ),
        )
        span = next(s for s in otel_spans.get_finished_spans() if s.name == SPAN_STT_USAGE)
        assert json.loads(span.attributes["langfuse.observation.usage_details"]) == {
            "stt_audio_ms": 5000
        }
```

Add `import json` and `from typing import Any` to the file's imports if not already present.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_cascade.py::TestUsageSpanWiring -v`
Expected: FAIL with `StopIteration` — no usage span was produced.

- [ ] **Step 3: Bind STT/TTS to locals and attach**

In `cascade.py`, add to the imports:

```python
from opentelemetry.context import Context

from vera_core.observability import attach_usage_spans
```

Replace `build_session` (`cascade.py:127-153`) with:

```python
def build_session(
    vad: Any | None = None,
    *,
    key_terms: list[str] | None = None,
    llm_model: str | None = None,
    thinking_override: dict[str, Any] | None = None,
    default_model: str,
    room_name: str | None = None,
    parent_context: Context | None = None,
) -> AgentSession[TakeoverState]:
    model = resolve_llm_model(llm_model, default_model)
    # STT/TTS are bound to locals (rather than passed inline) so usage-span listeners
    # can be attached: LiveKit reports their usage through channels Langfuse never
    # sees, so without this their spend is invisible. The LLM needs no listener — the
    # SDK already sets the gen_ai.usage.* attributes Langfuse prices.
    stt = deepgram.STTv2(
        model="flux-general-en", eager_eot_threshold=0.5, **stt_kwargs(key_terms)
    )
    tts = cartesia.TTS(model=_CARTESIA_TTS_MODEL, emotion=["confident"])
    attach_usage_spans(stt, parent_context=parent_context, room_name=room_name)
    attach_usage_spans(tts, parent_context=parent_context, room_name=room_name)
    # The latch must exist from construction: agents read it before speaking or hanging up.
    return AgentSession(
        userdata=TakeoverState(),
        stt=stt,
        llm=google.LLM(
            model=model,
            vertexai=True,
            location="global",
            thinking_config=resolve_thinking_config(model, thinking_override),
        ),
        tts=tts,
        vad=vad if vad is not None else _build_vad(),
        **cascade_session_kwargs(turn_detector=EnglishModel()),
    )
```

- [ ] **Step 4: Capture the context and publish the trace link in main.py**

In `main.py`, add to the imports:

```python
from opentelemetry import context as otel_context

from vera_core.observability import TraceLinkStore, current_traceparent
```

and add `attach_usage_spans` to the existing `vera_core.observability` import.

Immediately after the existing `trace.get_current_span().set_attributes(call_trace_attributes(room_name))` (`main.py:387`), insert:

```python
        # Captured HERE, where LiveKit's job_entrypoint span is genuinely ambient.
        # Usage-span listeners are registered from code that later runs in other tasks
        # — notably the takeover STT, reached via a room-event callback whose task does
        # NOT carry this context. Capturing now and closing over the value keeps every
        # usage span inside this call's trace; reading ambient context at emit time
        # would silently produce new trace roots that never sum into the call's cost.
        usage_parent_ctx = otel_context.get_current()
        # Publish this call's trace id so the control plane's later spans (post-call
        # eval, summary, coaching whisper) join THIS trace rather than forming their
        # own. Langfuse's per-trace cost rollup is what makes a per-call total real.
        if events_redis is not None and (traceparent := current_traceparent()):
            await TraceLinkStore(events_redis).publish(room_name, traceparent)
```

`events_redis` is created just above for canonical rooms only (`main.py:365-370`) and closed in the existing teardown, so this adds no new lifecycle. A foreign/console room has no `events_redis` and needs no link.

Then pass the context into the `build_session` call (`main.py:479-485`):

```python
        session = build_session(
            vad=ctx.proc.userdata.get("vad"),
            key_terms=controller.plan.stt_key_terms if controller is not None else None,
            llm_model=meta.get("llm_model_override"),
            thinking_override=meta.get("llm_thinking_override"),
            default_model=settings.voice_llm_default_model,
            room_name=room_name,
            parent_context=usage_parent_ctx,
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_cascade.py -v`
Expected: PASS (including the pre-existing cascade tests)

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/cascade.py \
        vera-backend/apps/agent_worker/src/agent_worker/main.py \
        vera-backend/apps/agent_worker/tests/unit/test_cascade.py
git commit -m "feat(otel): price the cascade's Deepgram STT and Cartesia TTS, and publish the call's trace link"
```

---

### Task 5: Wire the per-track takeover STT

A takeover transcribes two channels (the callee and an intervening supervisor) with a fresh STT per track, all billed and all invisible today. The factory is widened so each generation can say which channel it billed.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/takeover_transcript.py:92,138`
- Modify: `apps/agent_worker/src/agent_worker/main.py:602-610`
- Test: `tests/unit/agent_worker/test_takeover_transcript.py` (extend)

**Interfaces:**
- Consumes: `attach_usage_spans` (Task 2); `usage_parent_ctx` (Task 4, same `main.py` scope).
- Produces: `TakeoverTranscriber(..., stt_factory: Callable[[SpeakerAttribution], agents_stt.STT[Any]], ...)` — a **breaking signature change**; the factory now receives the attribution.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/agent_worker/test_takeover_transcript.py`:

```python
class TestSTTFactoryAttribution:
    def test_factory_receives_the_attribution_for_the_track(self) -> None:
        """The generation must be able to say WHICH channel it billed — a takeover runs
        two concurrent Deepgram streams (callee + intervening supervisor), and both are
        real spend."""
        from agent_worker.takeover_transcript import SpeakerAttribution

        seen: list[SpeakerAttribution] = []

        def factory(attribution: SpeakerAttribution) -> Any:
            seen.append(attribution)
            return _FakeSTT()

        _drive_one_track(stt_factory=factory)
        assert [a.source for a in seen] == [SOURCE_REP]
```

**Read the existing tests in this file first and reuse their harness** — `_FakeSTT` and a helper that drives one subscribed track through `TakeoverTranscriber` — rather than adding a parallel one. If no such harness exists, build the minimal one: a `TakeoverTranscriber` over a stub `rtc.Room` whose `remote_participants` yields one audio publication for `callee_identity`, then call `.start()`. Import `SOURCE_REP` from **`vera_core.transcript`** — that is where `takeover_transcript.py:28-33` imports it from, alongside `SOURCE_SUPERVISOR` and `TurnSource`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/agent_worker/test_takeover_transcript.py::TestSTTFactoryAttribution -v`
Expected: FAIL with `TypeError: factory() takes 1 positional argument but 0 were given`

- [ ] **Step 3: Widen the factory type and pass the attribution**

In `takeover_transcript.py`, change the `__init__` parameter annotation (`:92`) from:

```python
        stt_factory: Callable[[], agents_stt.STT[Any]],
```

to:

```python
        # Receives the track's attribution so the caller can tag per-channel usage
        # generations (a takeover bills two concurrent STT streams).
        stt_factory: Callable[[SpeakerAttribution], agents_stt.STT[Any]],
```

And in `_transcribe_track` (`:138`) change:

```python
        stream = self._stt_factory().stream()
```

to:

```python
        stream = self._stt_factory(attribution).stream()
```

- [ ] **Step 4: Replace the lambda in main.py**

In `main.py`, extend the existing takeover import:

```python
from agent_worker.takeover_transcript import SpeakerAttribution, TakeoverTranscriber
```

Then replace the `TakeoverTranscriber(...)` construction (`main.py:602-610`) with:

```python
        takeover_transcriber: TakeoverTranscriber | None = None
        if turn_sink is not None and speaker is not None:

            def _takeover_stt(attribution: SpeakerAttribution) -> Any:
                # One STT per subscribed track, so one listener per track. The parent
                # context is the entrypoint's, NOT the ambient one: a supervisor who
                # joins after takeover starts arrives via room.on("track_subscribed"),
                # whose task does not carry the entrypoint span (design §3.5).
                stt = deepgram.STT(model="nova-3")
                attach_usage_spans(
                    stt,
                    parent_context=usage_parent_ctx,
                    room_name=room_name,
                    source=attribution.source,
                )
                return stt

            takeover_transcriber = TakeoverTranscriber(
                ctx.room,
                turn_sink,
                room_name,
                stt_factory=_takeover_stt,
                callee_identity=speaker.identity,
            )
```

`attribution.user_id` is deliberately NOT attached — it adds nothing to cost (spec §8 prohibition 2).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/agent_worker/test_takeover_transcript.py -v`
Expected: PASS. Fix any pre-existing test that constructs `TakeoverTranscriber` with a zero-arg factory by giving its lambda an ignored parameter (`lambda _attribution: ...`).

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/takeover_transcript.py \
        vera-backend/apps/agent_worker/src/agent_worker/main.py \
        vera-backend/tests/unit/agent_worker/test_takeover_transcript.py
git commit -m "feat(otel): price the per-track takeover STT, tagged by channel"
```

---

### Task 6: Wire coaching's hold-to-whisper STT

Whisper STT runs in the control plane. It joins the call's trace via the Task 3 link, so its spend is attributable to the call.

**Files:**
- Modify: `packages/vera_core/src/vera_core/stt.py:184-186`
- Modify: `apps/control_plane/src/control_plane/api/v1/coaching.py` (imports + the `transcribe()` call)
- Modify: `apps/control_plane/src/control_plane/deps.py`, `apps/control_plane/src/control_plane/main.py`
- Test: `tests/unit/observability/test_usage_spans.py` (append)

**Interfaces:**
- Consumes: `attach_usage_spans` (Task 2); `TraceLinkStore` (Task 3); `room_name_for_call`, `call_trace_attributes`.
- Produces: `get_trace_link_store(request: Request) -> TraceLinkStore` in `deps.py`; `app.state.trace_link_store`; a `vera.coaching.whisper` span under which `vera.stt.usage` nests via ambient context.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/observability/test_usage_spans.py`:

```python
class TestResilientSTTChain:
    def test_the_fallback_chain_emits_usage_generations(self, otel_spans: Any) -> None:
        """Whisper STT is real Deepgram spend. Attaching to the FallbackAdapter (not
        each inner STT) is deliberate: it re-emits the inner STTMetrics verbatim
        (stt/fallback_adapter.py:294), so the model name stays the true provider's
        rather than the literal 'FallbackAdapter'."""
        from vera_core.stt import ResilientSTT, STTSpec

        chain = _FakeEmitter()
        stt = ResilientSTT(STTSpec("deepgram", "flux-general-en"))
        stt._chain = chain  # the lazily-built FallbackAdapter
        attach_usage_spans(chain)

        chain.emit("metrics_collected", _stt())
        assert _only(otel_spans, SPAN_STT_USAGE).attributes[
            "langfuse.observation.model.name"
        ] == "flux-general-en"
```

If reaching into `_chain` reads as too invasive, assert via `_adapter()` with a stub `registry` returning `_FakeEmitter()` instances — `ResilientSTT.__init__` already accepts a `registry` override for exactly this.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py::TestResilientSTTChain -v`
Expected: FAIL with `StopIteration` — nothing attaches inside `_adapter()` yet.

- [ ] **Step 3: Attach to the chain in `_adapter()`**

In `packages/vera_core/src/vera_core/stt.py`, add to the imports:

```python
from vera_core.observability import attach_usage_spans
```

In `_adapter()`, replace:

```python
            self._stts = stts
            self._chain = FallbackAdapter(self._stts)
        return self._chain
```

with:

```python
            self._stts = stts
            self._chain = FallbackAdapter(self._stts)
            # Whisper STT is billed Deepgram usage and was invisible in Langfuse.
            # Attach to the ADAPTER, not each inner STT: it re-emits the inner
            # STTMetrics verbatim, so metadata.model_name stays the real provider's
            # model rather than the literal "FallbackAdapter".
            #
            # No parent_context: the intended parent is the per-request
            # vera.coaching.whisper span, which does not exist yet at chain-construction
            # time. Ambient context at emit time is correct here — the metrics task is
            # created inside transcribe(), so it inherits the request's span. aclose()
            # drops the chain, so a rebuilt chain gets a fresh listener and there is no
            # double registration.
            attach_usage_spans(self._chain)
        return self._chain
```

- [ ] **Step 4: Expose a TraceLinkStore on the app**

In `apps/control_plane/src/control_plane/main.py`, inside the lifespan where the other Redis-backed services are built (near `app.state.post_call_bus = PostCallJobBus(_redis())`, `:232`), add:

```python
        # Lets every control-plane span for a call join the worker's trace for that
        # call, so Langfuse's per-trace cost rollup covers the whole call.
        app.state.trace_link_store = TraceLinkStore(_redis())
```

Add `from vera_core.observability import TraceLinkStore` to its imports.

In `apps/control_plane/src/control_plane/deps.py`, add:

```python
def get_trace_link_store(request: Request) -> TraceLinkStore:
    store: TraceLinkStore = request.app.state.trace_link_store
    return store
```

with `from vera_core.observability import TraceLinkStore` added to its imports.

- [ ] **Step 5: Open the per-request parent span**

In `apps/control_plane/src/control_plane/api/v1/coaching.py`, add to the imports:

```python
from opentelemetry import trace

from control_plane.deps import get_trace_link_store
from vera_core.observability import TraceLinkStore, call_trace_attributes, room_name_for_call
```

and near the module's other module-level constants:

```python
_tracer = trace.get_tracer("vera.control_plane.coaching")
```

Add a parameter to `on_demand_transcribe`'s signature, alongside the existing `whisper_stt` dependency:

```python
    trace_links: Annotated[TraceLinkStore, Depends(get_trace_link_store)],
```

Replace the transcribe block with:

```python
    room_name = room_name_for_call(tenant_id, call.id)
    try:
        # Joins the agent worker's trace for this call (published at the job entrypoint),
        # so whisper spend sums into the call's total. If the link is missing or expired
        # this is None and the span becomes its own trace root — degraded, not broken.
        # The nested vera.stt.usage generation inherits whichever we get.
        #
        # record_exception/set_status_on_exception are OFF: an STT provider error can
        # embed the request payload (the supervisor's audio), and both would copy its
        # message onto the span.
        with _tracer.start_as_current_span(
            "vera.coaching.whisper",
            context=await trace_links.resolve(room_name),
            attributes=call_trace_attributes(room_name),
            record_exception=False,
            set_status_on_exception=False,
        ):
            text = await whisper_stt.transcribe(
                audio_bytes, mime_type=audio.content_type or "audio/webm"
            )
    except STTUnavailableError as exc:
```

Keep the rest of the `except` body unchanged. If a `room_name` local already exists in this function, reuse it rather than redefining.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/ tests/unit/control_plane/ tests/unit/vera_core/test_stt.py -v`
Expected: PASS

- [ ] **Step 7: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/stt.py \
        vera-backend/apps/control_plane/src/control_plane/ \
        vera-backend/tests/unit/observability/test_usage_spans.py
git commit -m "feat(otel): price coaching's whisper STT inside its call's trace"
```

---

### Task 7: Trace the post-call eval's Vertex calls

`VertexLLMClient` calls `google.genai` directly, so it emits **zero** spans today — the largest untraced LLM spend in the system, since `judge()` fans out into concurrent chunks. `_generate` is the single chokepoint for both passes.

**Files:**
- Modify: `apps/control_plane/src/control_plane/llm.py` (imports, `VertexLLMClient.__init__`, `_generate`, `extract`, `_judge_chunk`)
- Modify: `apps/control_plane/src/control_plane/post_call_consumer.py:186` (`_process_job`)
- Modify: `apps/control_plane/src/control_plane/main.py` (pass the store into the consumer)
- Test: `tests/unit/control_plane/test_post_call_llm_spans.py`

**Interfaces:**
- Consumes: `TraceLinkStore` (Task 3); the usage-key constants from Task 1.
- Produces: `VertexLLMClient._generate(prompt: str, schema: dict[str, Any], *, pass_name: str) -> list[dict[str, Any]]`; span name `vera.eval.generate`; `PostCallConsumer(..., trace_links: TraceLinkStore | None = None)`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/control_plane/test_post_call_llm_spans.py`:

```python
"""The post-call eval's Vertex calls emit zero spans today (design §2.5) — the largest
untraced LLM spend in the system. These assert the generation they now emit."""

import json
from typing import Any

import pytest

from control_plane.llm import SPAN_EVAL_GENERATE, VertexLLMClient
from vera_core.observability.otel_testing import assert_no_phi_values


class _FakeUsage:
    def __init__(self, prompt: int, candidates: int, cached: int, thoughts: int) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.cached_content_token_count = cached
        self.thoughts_token_count = thoughts


class _FakeResponse:
    def __init__(self, text: str, usage: Any) -> None:
        self.text = text
        self.usage_metadata = usage


def _client(response: _FakeResponse) -> VertexLLMClient:
    client = VertexLLMClient.__new__(VertexLLMClient)
    client._model = "gemini-2.5-flash"
    client._semaphore = __import__("asyncio").Semaphore(4)

    class _Models:
        async def generate_content(self, **_: Any) -> _FakeResponse:
            return response

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    client._client = _Client()
    return client


def _span(exporter: Any) -> Any:
    return next(s for s in exporter.get_finished_spans() if s.name == SPAN_EVAL_GENERATE)


@pytest.mark.asyncio
class TestEvalGeneration:
    async def test_a_generation_is_emitted_with_token_usage(self, otel_spans: Any) -> None:
        client = _client(_FakeResponse("[]", _FakeUsage(8412, 611, 0, 0)))
        await client._generate("prompt", {}, pass_name="extract")
        usage = json.loads(_span(otel_spans).attributes["langfuse.observation.usage_details"])
        assert usage == {"input": 8412, "output": 611}

    async def test_cached_tokens_are_split_out_of_input(self, otel_spans: Any) -> None:
        # prompt_token_count INCLUDES cached tokens; sending it whole alongside `cached`
        # would double-count them (design §5.4).
        client = _client(_FakeResponse("[]", _FakeUsage(10000, 500, 9000, 0)))
        await client._generate("prompt", {}, pass_name="extract")
        usage = json.loads(_span(otel_spans).attributes["langfuse.observation.usage_details"])
        assert usage == {"input": 1000, "cached": 9000, "output": 500}
        assert usage["input"] + usage["cached"] == 10000

    async def test_thinking_tokens_bill_as_output(self, otel_spans: Any) -> None:
        # gemini-2.5-flash is a thinking model and Vera configures thinking on it.
        client = _client(_FakeResponse("[]", _FakeUsage(100, 20, 0, 80)))
        await client._generate("prompt", {}, pass_name="judge")
        usage = json.loads(_span(otel_spans).attributes["langfuse.observation.usage_details"])
        assert usage == {"input": 100, "output": 100}

    async def test_the_span_is_typed_a_generation_and_names_its_model(
        self, otel_spans: Any
    ) -> None:
        client = _client(_FakeResponse("[]", _FakeUsage(10, 2, 0, 0)))
        await client._generate("prompt", {}, pass_name="judge")
        attrs = _span(otel_spans).attributes
        assert attrs["langfuse.observation.type"] == "generation"
        assert attrs["langfuse.observation.model.name"] == "gemini-2.5-flash"
        assert attrs["vera.eval.pass"] == "judge"

    async def test_a_response_without_usage_metadata_omits_usage(self, otel_spans: Any) -> None:
        # A zero-cost generation is indistinguishable from a broken one, so send nothing.
        client = _client(_FakeResponse("[]", None))
        await client._generate("prompt", {}, pass_name="extract")
        assert "langfuse.observation.usage_details" not in _span(otel_spans).attributes

    async def test_no_prompt_or_completion_text_reaches_the_span(self, otel_spans: Any) -> None:
        # build_extract_prompt embeds the full transcript and the response carries
        # extracted answer values — the PHI-densest inputs in the system (design §8).
        transcript = "member id 1234 for Jane Doe"
        client = _client(_FakeResponse('[{"v": "Jane Doe"}]', _FakeUsage(10, 2, 0, 0)))
        await client._generate(f"Transcript: {transcript}", {}, pass_name="extract")
        assert_no_phi_values(_span(otel_spans), "Jane Doe", "member id 1234")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_post_call_llm_spans.py -v`
Expected: FAIL with `ImportError: cannot import name 'SPAN_EVAL_GENERATE'`

- [ ] **Step 3: Emit the generation in `_generate`**

In `apps/control_plane/src/control_plane/llm.py`, add to the imports:

```python
from opentelemetry import trace

from vera_core.observability.usage_spans import (
    GENERATION,
    OBSERVATION_MODEL_ATTR,
    OBSERVATION_TYPE_ATTR,
    USAGE_DETAILS_ATTR,
    USAGE_INPUT,
    USAGE_OUTPUT,
)
```

Add near the module's other constants:

```python
SPAN_EVAL_GENERATE = "vera.eval.generate"
USAGE_CACHED = "cached"

_tracer = trace.get_tracer("vera.control_plane.post_call_eval")


def _eval_usage(usage: Any) -> dict[str, int]:
    """Vertex token counts mapped onto Langfuse usage keys (design §5.4).

    `prompt_token_count` INCLUDES cached tokens, so `input` is reduced by them —
    sending both whole would double-count. Thinking tokens bill as output, and
    gemini-2.5-flash is a thinking model Vera configures thinking on. `input`/`output`
    match the vocabulary the SDK's own LLM spans use, so one model price entry prices
    both surfaces.
    """
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    prompt = getattr(usage, "prompt_token_count", 0) or 0
    output = (getattr(usage, "candidates_token_count", 0) or 0) + (
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    details: dict[str, int] = {}
    if prompt - cached:
        details[USAGE_INPUT] = prompt - cached
    if cached:
        details[USAGE_CACHED] = cached
    if output:
        details[USAGE_OUTPUT] = output
    return details
```

Replace `_generate` with:

```python
    async def _generate(
        self, prompt: str, schema: dict[str, Any], *, pass_name: str
    ) -> list[dict[str, Any]]:
        # This is the ONLY chokepoint for both eval passes, and it bypasses every
        # auto-instrumented path (raw google.genai, not a LiveKit plugin), so without
        # this span the post-call eval's whole Vertex bill is invisible.
        #
        # record_exception/set_status_on_exception are OFF and no prompt/response text
        # is attached: the prompt embeds the full transcript and the response carries
        # extracted answer values (design §8).
        with _tracer.start_as_current_span(
            SPAN_EVAL_GENERATE,
            attributes={
                OBSERVATION_TYPE_ATTR: GENERATION,
                OBSERVATION_MODEL_ATTR: self._model,
                "gen_ai.request.model": self._model,
                "gen_ai.provider.name": "google",
                "vera.eval.pass": pass_name,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            async with self._semaphore:
                resp = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                )
            # A response with no usage metadata gets NO usage_details rather than zeros:
            # a zero-cost generation is indistinguishable from a broken one.
            if usage := _eval_usage(getattr(resp, "usage_metadata", None)):
                span.set_attribute(USAGE_DETAILS_ATTR, json.dumps(usage))
            return self._loads_response(resp.text)
```

Update the two call sites to pass `pass_name`:

```python
        data = await self._generate(
            build_extract_prompt(field_paths, turns, special_values),
            _EXTRACT_SCHEMA,
            pass_name="extract",
        )
```

```python
        data = await self._generate(
            build_judge_prompt(list(chunk), turns_block),
            _judge_schema(chunk_paths),
            pass_name="judge",
        )
```

- [ ] **Step 4: Run the job under the call's trace**

In `apps/control_plane/src/control_plane/post_call_consumer.py`, add to the imports:

```python
from opentelemetry import trace

from vera_core.observability import TraceLinkStore, call_trace_attributes, room_name_for_call
```

and near the module's other constants:

```python
_tracer = trace.get_tracer("vera.control_plane.post_call")
```

Add a `trace_links: TraceLinkStore | None = None` keyword parameter to `PostCallConsumer.__init__`, storing it as `self._trace_links = trace_links`. Then wrap `_process_job`'s body:

```python
    async def _process_job(self, job: PostCallJob) -> None:
        room_name = room_name_for_call(job.tenant_id, job.call_id)
        parent = None
        if self._trace_links is not None:
            # Joins the worker's trace for this call, so every eval generation below
            # sums into that call's total cost. A missing/expired link yields None and
            # this becomes its own trace root — degraded, not broken.
            parent = await self._trace_links.resolve(room_name)
        with _tracer.start_as_current_span(
            "vera.post_call.eval",
            context=parent,
            attributes=call_trace_attributes(room_name),
            record_exception=False,
            set_status_on_exception=False,
        ):
            turns = await build_turns(
                self._call_stream, self._sessionmaker, job.tenant_id, job.call_id
            )
            async with tenant_session(self._sessionmaker, job.tenant_id) as session:
                ...  # the remainder of the existing body, indented one level
```

Keep the existing body verbatim, just indented under the `with`.

In `main.py`, pass the store when constructing `PostCallConsumer` (near `:320`):

```python
                trace_links=app.state.trace_link_store,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/ -v`
Expected: PASS

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/llm.py \
        vera-backend/apps/control_plane/src/control_plane/post_call_consumer.py \
        vera-backend/apps/control_plane/src/control_plane/main.py \
        vera-backend/tests/unit/control_plane/test_post_call_llm_spans.py
git commit -m "feat(otel): trace the post-call eval's Vertex calls inside their call's trace"
```

---

### Task 8: Join the call summary to its call's trace

The summary already gets an auto-instrumented `llm_request` span from the SDK, but it forms an **orphan root trace** with no correlation to the call it summarizes.

**Files:**
- Modify: `apps/control_plane/src/control_plane/call_summary.py` (imports, `summarize_call`)
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py:595-628` (pass the store through)
- Test: `tests/unit/control_plane/` — extend the existing call-summary test module

**Interfaces:**
- Consumes: `TraceLinkStore` (Task 3), `get_trace_link_store` (Task 6).
- Produces: `summarize_call(..., trace_links: TraceLinkStore | None = None)`.

- [ ] **Step 1: Write the failing test**

Append to the existing call-summary unit test module (find it with `rg -l "summarize_call" vera-backend/tests/unit`):

```python
class TestSummaryTraceJoin:
    @pytest.mark.asyncio
    async def test_the_summary_span_joins_the_calls_trace(self, otel_spans: Any) -> None:
        """Without this the summary's LLM span is an orphan root trace — real spend
        that no per-call cost query can ever find."""
        from opentelemetry import trace

        from vera_core.observability import TraceLinkStore, current_traceparent

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_trace_id = worker_span.get_span_context().trace_id

        redis = _FakeRedis()  # reuse this module's fake, or the one from test_trace_link
        store = TraceLinkStore(redis)
        assert traceparent is not None
        await store.publish(room_name_for_call(TENANT_ID, CALL_ID), traceparent)

        await summarize_call(
            llm=_StubLLM(),
            cache=_StubCache(),
            stream=_StubStream(),
            sessionmaker=None,
            tenant_id=TENANT_ID,
            call_id=CALL_ID,
            ttl_seconds=5,
            trace_links=store,
        )
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.call_summary"
        )
        assert span.context.trace_id == worker_trace_id
```

Reuse the module's existing stubs for `llm` / `cache` / `stream` and its `TENANT_ID` / `CALL_ID` constants; the stub LLM must return enough turns that the summary is not short-circuited as `pending` (`_MIN_SPEECH_TURNS`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane -k SummaryTraceJoin -v`
Expected: FAIL — `summarize_call() got an unexpected keyword argument 'trace_links'`

- [ ] **Step 3: Open the summary span**

In `call_summary.py`, add to the imports:

```python
from opentelemetry import trace

from vera_core.observability import TraceLinkStore, call_trace_attributes
```

and near the module's other constants:

```python
_tracer = trace.get_tracer("vera.control_plane.summary")
```

Add `trace_links: TraceLinkStore | None = None` to `summarize_call`'s keyword parameters, and wrap the LLM call:

```python
    # The SDK auto-instruments the LLM request, but as a root trace with no link to the
    # call — real spend that no per-call cost query can find. This parents it into the
    # call's own trace; a missing/expired link degrades to a root span.
    parent = await trace_links.resolve(room_name) if trace_links is not None else None
    with _tracer.start_as_current_span(
        "vera.call_summary",
        context=parent,
        attributes=call_trace_attributes(room_name),
        record_exception=False,
        set_status_on_exception=False,
    ):
        reply = await llm.complete(system=SUMMARY_SYSTEM_PROMPT, user=format_diarized(turns))
```

`record_exception` is off for the same reason as elsewhere: the prompt is the diarized transcript.

- [ ] **Step 4: Pass the store from the endpoint**

In `apps/control_plane/src/control_plane/api/v1/calls.py`, add to `get_call_summary`'s signature:

```python
    trace_links: Annotated[TraceLinkStore, Depends(get_trace_link_store)],
```

with `from control_plane.deps import get_trace_link_store` and `from vera_core.observability import TraceLinkStore` added to the imports, then pass `trace_links=trace_links` into the `summarize_call(...)` call.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/ -v`
Expected: PASS

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/call_summary.py \
        vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py \
        vera-backend/tests/unit/control_plane/
git commit -m "feat(otel): join the call summary's LLM span to its call's trace"
```

---

### Task 9: Correct the LLM cached-token split at export

Every LLM span Vera emits today bills cache hits at the full input rate. The Google plugin reports them, the SDK collects them into `LLMMetrics.prompt_cached_tokens`, and the span drops them — so cost is **overstated in proportion to hit rate**, worst on the longest calls. The span is not ours to fix at the source, and a sibling generation would be summed and double-count, so it is corrected in place at export.

**Files:**
- Create: `packages/vera_core/src/vera_core/observability/llm_usage_export.py`
- Modify: `packages/vera_core/src/vera_core/observability/otel.py`
- Test: `tests/unit/observability/test_llm_usage_export.py`

**Interfaces:**
- Consumes: the usage-key constants from Task 1.
- Produces: `class UsageEnrichingExporter(SpanExporter)` wrapping any `SpanExporter`; `corrected_usage_details(raw_metrics: str) -> dict[str, int] | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/test_llm_usage_export.py`:

```python
"""The SDK measures LLM cache hits and then discards them (design §2.6): the Google
plugin sets prompt_cached_tokens, LLMMetrics carries it, and llm_request sets only
input/output — so Langfuse prices cache hits at the full input rate and every LLM
figure is overstated in proportion to hit rate."""

import json
from typing import Any

from vera_core.observability.llm_usage_export import (
    UsageEnrichingExporter,
    corrected_usage_details,
)


def _metrics(prompt: int, cached: int, completion: int) -> str:
    return json.dumps(
        {
            "prompt_tokens": prompt,
            "prompt_cached_tokens": cached,
            "completion_tokens": completion,
        }
    )


class TestCorrectedUsage:
    def test_cached_tokens_are_split_out_of_input(self) -> None:
        assert corrected_usage_details(_metrics(12480, 9360, 210)) == {
            "input": 3120,
            "cached": 9360,
            "output": 210,
        }

    def test_input_plus_cached_reconstructs_the_original_prompt_count(self) -> None:
        # THE invariant. Drop the subtraction and cached is double-counted; drop the
        # cached key while still subtracting and the hits vanish. Both are silent.
        usage = corrected_usage_details(_metrics(12480, 9360, 210))
        assert usage is not None
        assert usage["input"] + usage["cached"] == 12480

    def test_no_cache_hits_omits_the_cached_key(self) -> None:
        assert corrected_usage_details(_metrics(500, 0, 20)) == {"input": 500, "output": 20}

    def test_a_malformed_blob_yields_none(self) -> None:
        assert corrected_usage_details("not json") is None
        assert corrected_usage_details("[]") is None

    def test_every_value_is_an_int(self) -> None:
        usage = corrected_usage_details(_metrics(12480, 9360, 210))
        assert usage is not None
        assert all(isinstance(v, int) for v in usage.values())


class _RecordingExporter:
    def __init__(self) -> None:
        self.exported: list[Any] = []
        self.shutdown_called = False
        self.flushed = False

    def export(self, spans: Any) -> Any:
        self.exported.extend(spans)
        return None

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushed = True
        return True


class _FakeSpan:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self._attributes = attributes

    @property
    def attributes(self) -> dict[str, Any]:
        return self._attributes


class TestExporter:
    def test_an_llm_span_gains_corrected_usage_details(self) -> None:
        inner = _RecordingExporter()
        span = _FakeSpan({"lk.llm_metrics": _metrics(12480, 9360, 210)})
        UsageEnrichingExporter(inner).export([span])
        attrs = inner.exported[0].attributes
        assert json.loads(attrs["langfuse.observation.usage_details"]) == {
            "input": 3120,
            "cached": 9360,
            "output": 210,
        }
        assert attrs["langfuse.observation.type"] == "generation"

    def test_a_span_without_llm_metrics_passes_through_untouched(self) -> None:
        inner = _RecordingExporter()
        span = _FakeSpan({"vera.room": "call--a--b"})
        UsageEnrichingExporter(inner).export([span])
        assert inner.exported == [span]

    def test_a_malformed_blob_exports_the_original_span(self) -> None:
        # An SDK upgrade renaming or reshaping lk.llm_metrics must degrade to today's
        # behavior. Losing a span is worse than exporting an uncorrected one.
        inner = _RecordingExporter()
        span = _FakeSpan({"lk.llm_metrics": "not json"})
        UsageEnrichingExporter(inner).export([span])
        assert inner.exported == [span]

    def test_lifecycle_calls_are_delegated(self) -> None:
        # Without delegation, spans queued at process exit are silently lost.
        inner = _RecordingExporter()
        exporter = UsageEnrichingExporter(inner)
        exporter.force_flush()
        exporter.shutdown()
        assert inner.flushed and inner.shutdown_called
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_llm_usage_export.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'vera_core.observability.llm_usage_export'`

- [ ] **Step 3: Write the module**

Create `packages/vera_core/src/vera_core/observability/llm_usage_export.py`:

```python
"""Correct the SDK's LLM spans so cache hits are not billed at the full input rate.

The Google plugin reports `cached_content_token_count` and livekit-agents carries it
as `LLMMetrics.prompt_cached_tokens`, but the `llm_request` span sets only
`gen_ai.usage.input_tokens` / `output_tokens` — and `prompt_tokens` INCLUDES the
cached ones. Langfuse therefore prices every cache hit at the full input rate, so
Vera's LLM cost is overstated in proportion to the hit rate. The voice cascade
re-sends a growing chat context every turn, which is the archetypal implicit-cache
workload, so the error is worst on the longest calls.

The fix cannot be a Vera-owned span: a second generation for the same request would be
summed by Langfuse and double-count it. It must correct the SAME span. Two properties
make that exact:

- the span already carries everything needed, in `lk.llm_metrics`;
- `langfuse.*` attributes take precedence over `gen_ai.*`, so adding
  `langfuse.observation.usage_details` overrides the SDK's uncorrected counts.

Additive and keyed on one attribute: a span without `lk.llm_metrics` is untouched, and
a malformed or renamed blob exports the ORIGINAL span. Losing a span is worse than
exporting an uncorrected one.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from vera_core.observability.usage_spans import (
    GENERATION,
    OBSERVATION_TYPE_ATTR,
    USAGE_DETAILS_ATTR,
    USAGE_INPUT,
    USAGE_OUTPUT,
)

logger = logging.getLogger("vera.observability")

# The SDK's own metrics blob (livekit.agents.telemetry.trace_types.ATTR_LLM_METRICS).
LLM_METRICS_ATTR = "lk.llm_metrics"
USAGE_CACHED = "cached"


def corrected_usage_details(raw_metrics: str) -> dict[str, int] | None:
    """Usage keys for one `lk.llm_metrics` blob, or None when it is unusable.

    `input` is reduced by the cached count because `prompt_tokens` includes it —
    sending both whole would double-count the hits. `input + cached` therefore always
    reconstructs the SDK's original `prompt_tokens`.
    """
    try:
        metrics = json.loads(raw_metrics)
    except (TypeError, ValueError):
        return None
    if not isinstance(metrics, dict):
        return None
    prompt = int(metrics.get("prompt_tokens", 0) or 0)
    cached = int(metrics.get("prompt_cached_tokens", 0) or 0)
    completion = int(metrics.get("completion_tokens", 0) or 0)
    details: dict[str, int] = {}
    if prompt - cached:
        details[USAGE_INPUT] = prompt - cached
    if cached:
        details[USAGE_CACHED] = cached
    if completion:
        details[USAGE_OUTPUT] = completion
    return details or None


def _enrich(span: ReadableSpan) -> ReadableSpan:
    attributes = span.attributes or {}
    raw = attributes.get(LLM_METRICS_ATTR)
    if not isinstance(raw, str):
        return span
    usage = corrected_usage_details(raw)
    if usage is None:
        return span
    # ReadableSpan exposes attributes read-only, so re-project rather than mutate.
    merged = dict(attributes)
    merged[USAGE_DETAILS_ATTR] = json.dumps(usage)
    merged[OBSERVATION_TYPE_ATTR] = GENERATION
    return ReadableSpan(
        name=span.name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource,
        attributes=merged,
        events=span.events,
        links=span.links,
        status=span.status,
        kind=span.kind,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class UsageEnrichingExporter(SpanExporter):
    """Wraps a SpanExporter, correcting LLM spans' usage on the way out."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        enriched: list[ReadableSpan] = []
        for span in spans:
            try:
                enriched.append(_enrich(span))
            except Exception as exc:
                # Never drop a span over a correction failure.
                logger.warning("llm usage enrichment failed: %s", type(exc).__name__)
                enriched.append(span)
        result: SpanExportResult = self._wrapped.export(enriched)
        return result

    def shutdown(self) -> None:
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        flushed: bool = self._wrapped.force_flush(timeout_millis)
        return flushed
```

- [ ] **Step 4: Wrap the exporter in `configure_observability`**

In `packages/vera_core/src/vera_core/observability/otel.py`, add to the local imports inside `configure_observability`:

```python
    from vera_core.observability.llm_usage_export import UsageEnrichingExporter
```

and replace:

```python
    provider.add_span_processor(BatchSpanProcessor(exporter))
```

with:

```python
    # Corrects the SDK's llm_request spans so Gemini cache hits are not billed at the
    # full input rate (see llm_usage_export). Every other span passes through untouched.
    provider.add_span_processor(BatchSpanProcessor(UsageEnrichingExporter(exporter)))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/ -v`
Expected: PASS

- [ ] **Step 6: Lint and typecheck**

Run: `cd vera-backend && uv run ruff format --check . && uv run ruff check . && uv run mypy`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/observability/ \
        vera-backend/tests/unit/observability/test_llm_usage_export.py
git commit -m "fix(otel): stop billing Gemini cache hits at the full input rate"
```

---

### Task 10: The price seeder, the docs, and the full gate

Without matching model price entries the usage attributes ingest fine and every observation renders blank cost. This creates them, documents the manual fallback, and runs the one full `just check` for the whole plan.

**Files:**
- Create: `scripts/seed_langfuse_prices.py`
- Create: `docs/superpowers/specs/2026-08-17-langfuse-price-entry-runbook.md` (repo root)
- Modify: `justfile` (after `langfuse-down`), `adr/devops-todo.md`
- Test: `tests/unit/observability/test_seed_langfuse_prices.py`

**Interfaces:**
- Consumes: `get_settings()` for `langfuse_host` / `langfuse_public_key` / `langfuse_secret_key`; usage-key constants from Tasks 1, 7 and 9.
- Produces: `MODELS: tuple[ModelPrice, ...]`, `MissingRateError`, `resolve_rates(env: Mapping[str, str]) -> dict[str, float]`, `build_payload(price: ModelPrice, model_id: str | None, *, rates: Mapping[str, float]) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/test_seed_langfuse_prices.py`:

```python
"""Seeder contract: idempotent upsert, never seed a zero, and never ship a Gemini
entry without a cached rate."""

import re
from typing import Any

import pytest

from scripts.seed_langfuse_prices import (
    MODELS,
    MissingRateError,
    build_payload,
    resolve_rates,
)

_RATES = {
    "LANGFUSE_PRICE_STT_FLUX_PER_MS": "0.00000010833",
    "LANGFUSE_PRICE_STT_NOVA_PER_MS": "0.00000012833",
    "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER": "0.000022",
    "LANGFUSE_PRICE_LLM_GEMINI_INPUT_PER_TOKEN": "0.0000003",
    "LANGFUSE_PRICE_LLM_GEMINI_OUTPUT_PER_TOKEN": "0.0000025",
    "LANGFUSE_PRICE_LLM_GEMINI_CACHED_PER_TOKEN": "0.000000075",
}


class TestRates:
    def test_every_model_rate_resolves_from_env(self) -> None:
        rates = resolve_rates(_RATES)
        for model in MODELS:
            for env_var in model.env_vars.values():
                assert env_var in rates

    def test_a_missing_rate_refuses_to_seed(self) -> None:
        # A $0.00 entry is indistinguishable from broken instrumentation in the UI, so
        # a partial seed is worse than no seed.
        with pytest.raises(MissingRateError):
            resolve_rates({k: v for k, v in _RATES.items() if "FLUX" not in k})

    def test_a_missing_cached_rate_refuses_to_seed(self) -> None:
        # Omitting it silently prices cache hits at $0 — the mirror image of the bug
        # Task 9 fixes, understating cost instead of overstating it.
        with pytest.raises(MissingRateError):
            resolve_rates({k: v for k, v in _RATES.items() if "CACHED" not in k})

    def test_an_unparseable_rate_refuses_to_seed(self) -> None:
        with pytest.raises(MissingRateError):
            resolve_rates({**_RATES, "LANGFUSE_PRICE_STT_FLUX_PER_MS": "cheap"})


class TestPayload:
    def test_a_new_model_carries_no_model_id(self) -> None:
        payload = build_payload(MODELS[0], None, rates=resolve_rates(_RATES))
        assert "modelId" not in payload
        assert payload["modelName"] == MODELS[0].model_name

    def test_an_existing_model_threads_its_id_back_in(self) -> None:
        # POST /api/public/models upserts ONLY when given an existing modelId; a
        # duplicate modelName without one is rejected on (projectId, modelName).
        payload = build_payload(MODELS[0], "clx123", rates=resolve_rates(_RATES))
        assert payload["modelId"] == "clx123"

    def test_prices_use_the_usage_keys_the_instrumentation_sends(self) -> None:
        rates = resolve_rates(_RATES)
        by_name = {m.model_name: m for m in MODELS}
        flux = build_payload(by_name["vera-deepgram-flux"], None, rates=rates)
        assert set(flux["pricingTiers"][0]["prices"]) == {"stt_audio_ms"}
        gemini = build_payload(by_name["vera-gemini"], None, rates=rates)
        assert set(gemini["pricingTiers"][0]["prices"]) == {"input", "output", "cached"}

    def test_patterns_match_the_models_vera_actually_uses(self) -> None:
        by_name = {m.model_name: m for m in MODELS}
        assert re.match(by_name["vera-deepgram-flux"].match_pattern, "flux-general-en")
        assert re.match(by_name["vera-deepgram-nova"].match_pattern, "nova-3")
        assert re.match(
            by_name["vera-cartesia-sonic"].match_pattern, "sonic-3.5-2026-05-04"
        )
        assert re.match(by_name["vera-gemini"].match_pattern, "gemini-2.5-flash")
        assert re.match(by_name["vera-gemini"].match_pattern, "gemini-3.1-flash-lite")

    def test_patterns_survive_a_model_version_bump(self) -> None:
        # Family patterns, not exact versions: an exact pattern would silently zero cost
        # on the next bump, and a missing match looks identical to "no data".
        by_name = {m.model_name: m for m in MODELS}
        assert re.match(by_name["vera-cartesia-sonic"].match_pattern, "sonic-4")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_seed_langfuse_prices.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.seed_langfuse_prices'`

- [ ] **Step 3: Write the seeder**

Create `scripts/seed_langfuse_prices.py`:

```python
"""Seed the Langfuse model price entries Vera's usage attributes are priced against.

Vera's spans carry raw usage only (`langfuse.observation.usage_details`); Langfuse does
the arithmetic. It can only do so if a model definition exists whose per-usage-type
price keys match the usage keys we send — otherwise usage ingests fine and every
observation renders BLANK cost, which looks exactly like broken instrumentation.

Rates are read from the environment, never hardcoded and deliberately NOT in Settings:
the application never needs a price, so this keeps exactly one place prices live and no
second copy inside Vera to drift.

    just langfuse-seed-prices

    LANGFUSE_PRICE_STT_FLUX_PER_MS              Deepgram Flux, $ per MILLISECOND of audio
    LANGFUSE_PRICE_STT_NOVA_PER_MS              Deepgram Nova, $ per MILLISECOND of audio
    LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER      Cartesia Sonic, $ per character
    LANGFUSE_PRICE_LLM_GEMINI_INPUT_PER_TOKEN   Gemini, $ per uncached input token
    LANGFUSE_PRICE_LLM_GEMINI_OUTPUT_PER_TOKEN  Gemini, $ per output token
    LANGFUSE_PRICE_LLM_GEMINI_CACHED_PER_TOKEN  Gemini, $ per CACHED input token

The audio rates are PER MILLISECOND because Langfuse stores usage as integers and
fractional seconds are truncated. Published rates are per minute, so converting is
`per_minute / 60000` — entering the per-minute figure directly overstates cost by
60,000x while still rendering a plausible dollar amount.

Public list prices (~$0.0077/min Deepgram Nova streaming, ~$0.0065/min Flux, $5-37 per
million Cartesia characters) are a SANITY REFERENCE ONLY. Use your contracted rates.

Idempotent: `POST /api/public/models` upserts only when handed an existing `modelId`,
and rejects a duplicate `modelName` without one, so this GETs the model list first and
threads any existing id back in. Re-running is a no-op-shaped update.

Writes to whatever VERA_LANGFUSE_HOST resolves to — the target host is logged.
"""

import asyncio
import base64
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from vera_core.config import get_settings

logger = logging.getLogger("vera.seed_langfuse_prices")


class MissingRateError(RuntimeError):
    """A rate env var is absent or unparseable. Refuse to seed rather than write a
    $0.00 entry, which is indistinguishable from broken instrumentation in the UI."""


@dataclass(frozen=True)
class ModelPrice:
    model_name: str
    # A FAMILY regex, not an exact version: bumping sonic-3.5 -> sonic-4 must not
    # silently zero the cost, and a missing match renders identically to "no data".
    match_pattern: str
    # usage key -> env var holding its rate. Keys MUST equal what the instrumentation
    # puts in usage_details (vera_core.observability.usage_spans / llm_usage_export).
    env_vars: Mapping[str, str] = field(default_factory=dict)


MODELS: tuple[ModelPrice, ...] = (
    ModelPrice(
        "vera-deepgram-flux",
        r"(?i)^flux-.*$",
        {"stt_audio_ms": "LANGFUSE_PRICE_STT_FLUX_PER_MS"},
    ),
    ModelPrice(
        "vera-deepgram-nova",
        r"(?i)^nova-.*$",
        {"stt_audio_ms": "LANGFUSE_PRICE_STT_NOVA_PER_MS"},
    ),
    ModelPrice(
        "vera-cartesia-sonic",
        r"(?i)^sonic-.*$",
        {"tts_characters": "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER"},
    ),
    # One family entry covers every Gemini surface (cascade, observer, health, summary,
    # post-call eval). The `cached` key is REQUIRED: without it Langfuse prices cache
    # hits at $0 and understates cost — the mirror image of the bug the export-time
    # correction fixes.
    ModelPrice(
        "vera-gemini",
        r"(?i)^gemini-.*$",
        {
            "input": "LANGFUSE_PRICE_LLM_GEMINI_INPUT_PER_TOKEN",
            "output": "LANGFUSE_PRICE_LLM_GEMINI_OUTPUT_PER_TOKEN",
            "cached": "LANGFUSE_PRICE_LLM_GEMINI_CACHED_PER_TOKEN",
        },
    ),
)


def resolve_rates(env: Mapping[str, str]) -> dict[str, float]:
    """Every rate every model needs, or MissingRateError. All-or-nothing on purpose:
    a partially-priced project renders some observations at $0, which reads as
    "this surface is free" rather than "this seed was incomplete"."""
    rates: dict[str, float] = {}
    missing: list[str] = []
    for model in MODELS:
        for env_var in model.env_vars.values():
            raw = env.get(env_var)
            if raw is None:
                missing.append(env_var)
                continue
            try:
                rates[env_var] = float(raw)
            except ValueError:
                missing.append(env_var)
    if missing:
        raise MissingRateError(f"missing or unparseable rate env vars: {sorted(set(missing))}")
    return rates


def build_payload(
    price: ModelPrice, model_id: str | None, *, rates: Mapping[str, float]
) -> dict[str, Any]:
    """The POST body for one model entry. Prices go in pricingTiers[0].prices — the
    deprecated flat inputPrice/outputPrice cannot express a custom usage key at all."""
    payload: dict[str, Any] = {
        "modelName": price.model_name,
        "matchPattern": price.match_pattern,
        "pricingTiers": [
            {"prices": {key: rates[env_var] for key, env_var in price.env_vars.items()}}
        ],
    }
    if model_id is not None:
        payload["modelId"] = model_id
    return payload


async def _existing_ids(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/public/models", params={"limit": 100})
    response.raise_for_status()
    data = response.json().get("data", [])
    return {m["modelName"]: m["id"] for m in data if "modelName" in m and "id" in m}


async def seed(client: httpx.AsyncClient, rates: Mapping[str, float]) -> list[str]:
    """Upsert every entry; returns the model names written."""
    existing = await _existing_ids(client)
    written: list[str] = []
    for price in MODELS:
        payload = build_payload(price, existing.get(price.model_name), rates=rates)
        response = await client.post("/api/public/models", json=payload)
        response.raise_for_status()
        written.append(price.model_name)
        logger.info(
            "seeded %s (matchPattern=%s, usage keys=%s)",
            price.model_name,
            price.match_pattern,
            sorted(price.env_vars),
        )
    return written


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if not settings.langfuse_host:
        logger.error("VERA_LANGFUSE_HOST is not set — nothing to seed")
        return 1
    try:
        rates = resolve_rates(os.environ)
    except MissingRateError as exc:
        logger.error("%s", exc)
        logger.error("refusing to seed: a $0.00 price looks identical to broken tracing")
        return 1

    token = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    logger.info("seeding model prices into %s", settings.langfuse_host)
    async with httpx.AsyncClient(
        base_url=settings.langfuse_host.rstrip("/"),
        headers={"Authorization": f"Basic {token}"},
        timeout=30.0,
    ) as client:
        written = await seed(client, rates)
    logger.info("seeded %d model price entries: %s", len(written), ", ".join(written))
    # Configured selectors that match no entry above would render blank cost silently.
    logger.info(
        "verify these configured models match a pattern above: %s",
        sorted(
            {
                settings.voice_llm_default_model,
                settings.gemini_flash_model,
                settings.summary_primary_model,
                settings.observer_extract_primary_model,
                settings.health_primary_model,
                settings.whisper_stt_primary_model,
                *settings.summary_fallback_models,
                *settings.observer_extract_fallback_models,
                *settings.health_fallback_models,
                *settings.whisper_stt_fallback_models,
            }
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_seed_langfuse_prices.py -v`
Expected: PASS

- [ ] **Step 5: Add the just recipe**

In `vera-backend/justfile`, after the `langfuse-down` recipe:

```make
# Seed the Langfuse model price entries Vera's usage attributes are priced against.
# Idempotent; refuses to run without every rate env var set (see the script header —
# note the audio rates are PER MILLISECOND, i.e. published per-minute / 60000).
langfuse-seed-prices:
    uv run python scripts/seed_langfuse_prices.py
```

- [ ] **Step 6: Write the manual runbook**

Create `docs/superpowers/specs/2026-08-17-langfuse-price-entry-runbook.md` (repo root). It must contain:

- An opening pointer that `just langfuse-seed-prices` is the preferred path and this is the fallback (no shell access, adjusting one rate, a hand-provisioned project), so this doc does not quietly become the primary route and drift.
- The click path — Langfuse → project → **Settings → Models → + New model** — and which UI field maps to which API field (the UI says "match pattern" and "price" where the API says `matchPattern` and `pricingTiers[0].prices`).
- The four entries as a fill-in table: `modelName`, `matchPattern`, usage keys, price — copied from `MODELS` in the seeder.
- **A unit warning in the audio rows:** the price is **per millisecond**; published Deepgram rates are per minute, so divide by 60,000. Entering the per-minute figure overstates cost 60,000× and still looks plausible.
- **A note that the Gemini entry needs all three keys** (`input`, `output`, `cached`); omitting `cached` prices cache hits at $0 and understates cost.
- **How to discover the usage keys from a live observation** rather than trusting this doc: open any `vera.stt.usage` or `vera.tts.usage` generation and read the keys off its `usage_details`. Keeps the runbook self-correcting if attribute names change.
- A "cost is blank — why" triage table, since all five causes look identical in the UI: no model entry · `matchPattern` does not match the ingested model · price key ≠ usage key · usage key typo'd in the instrumentation · **the observation is a span, not a generation** (only `generation`/`embedding` carry cost).
- Public list prices as a sanity reference, flagged as *not* the contracted rate.

- [ ] **Step 7: Add the devops-todo row**

Append a row to `vera-backend/adr/devops-todo.md` (matching the file's existing column layout):

| # | Item | Why it matters | Source |
|---|---|---|---|
| _next_ | ☐ **Seed the Langfuse model price entries in every environment** — `just langfuse-seed-prices` with all six rate env vars set, creating `vera-deepgram-flux`, `vera-deepgram-nova`, `vera-cartesia-sonic` and `vera-gemini` with the real contracted rates (audio rates are **per millisecond**). Re-run after any Langfuse project re-provision (the entries live in Langfuse's own DB, not this repo) and after any STT/TTS/LLM model-family change. Manual fallback: `docs/superpowers/specs/2026-08-17-langfuse-price-entry-runbook.md`. | Usage attributes ingest fine without a price entry, but every observation then renders blank cost — so runaway spend and cost regressions stay invisible, and a missing entry is indistinguishable in the UI from broken instrumentation. The rates are contract-specific rather than public list price, so they cannot ship in code; they are not secrets, just values that must exist wherever the seeder runs. | Per-call provider cost observability (2026-08-17); spec `docs/superpowers/specs/2026-08-17-call-cost-observability-design.md`. |

- [ ] **Step 8: Run the `/simplify` skill**

Per repo `CLAUDE.md`, run the **code-simplifier** plugin on the whole change with the trigger phrase **"simplify code"**. It reconciles the change to clear, consistent, maintainable code without changing behavior.

- [ ] **Step 9: Run the FULL gate — the one time in this plan**

Run: `cd vera-backend && just check`
Expected: PASS (ruff check + ruff format --check + mypy --strict + the whole pytest suite).

This is the only full-suite run in the plan, and it must be on the exact tree being pushed. If it fails on something you believe predates this branch, prove it: run the same check on the merge-base and verify your own changed files pass in isolation (`git diff --name-only <base>...HEAD | xargs uv run ruff format --check`).

- [ ] **Step 10: Commit**

```bash
git add vera-backend/scripts/seed_langfuse_prices.py \
        vera-backend/tests/unit/observability/test_seed_langfuse_prices.py \
        vera-backend/justfile vera-backend/adr/devops-todo.md \
        docs/superpowers/specs/2026-08-17-langfuse-price-entry-runbook.md
git commit -m "feat(otel): seed Langfuse model prices for Deepgram, Cartesia and Gemini"
```

---

## After the plan: live verification is the definition of done

**Unit tests prove what Vera emits. They cannot prove Langfuse ingests, types, matches and prices it.** The spec makes a live run the definition of done (§10.3) — do not report this work complete on a green `just check` alone.

1. `just langfuse-up`; set the six rate env vars; `just langfuse-seed-prices`; confirm four entries under Settings → Models and read the coverage log line.
2. `just up`, `just api`, `just worker` (browser-callee transport — no telephony needed).
3. Place a **multi-turn** test call; join as supervisor and **Intervene**; fire **hold-to-whisper** once; request a **summary**; let the call end so **post-call eval** runs.
4. **Every** generation carries a non-blank `$` — checked across all of them, since an uncovered model family shows on some spans and not others. Takeover spans show both `vera.usage.source="rep"` and `="supervisor"`.
5. **The post-call eval and summary generations appear in the SAME trace as the call**, and that trace's total includes them. This also proves Langfuse accepts spans arriving after the trace's root span ended.
6. **Check the arithmetic by hand** — `audio_ms × rate ≈ displayed cost`, and the same for characters. The only step that catches a per-ms/per-second/per-minute mismatch. Confirm the stored usage is a whole number of milliseconds matching what Vera emitted.
7. **Confirm the cache split landed** — late-call `llm_request` generations show a non-zero `cached`, and `input + cached` equals the SDK's `gen_ai.usage.input_tokens`. A one- or two-turn call proves nothing: implicit caching needs a repeated prefix.

## Self-review notes

- **Spec coverage:** §3.1→T1/T2, §3.2→T3, §3.3 sites 1-2→T4, 3→T5, 4→T6, 5-6→T7, 7→T8, 8→T9, §3.4→T5, §3.5→T2, §3.6→T9, §5.1-5.2→T1, §5.3→T1, §5.4→T7, §5.5→T9, §6→every task's try/except, §7→T10, §8→T1/T2/T7 PHI tests, §10.3→the section above. Spec §9 (the `ResilientLLM` debt) is explicitly a non-goal and has no task, by design.
- **Type consistency:** `attach_usage_spans` keeps one signature across T2/T4/T5/T6. `usage_details` keys are `input`/`output`/`cached` in T7 and T9 and `stt_audio_ms`/`tts_characters` in T1, and T10's seeder prices exactly those strings. `TraceLinkStore.resolve` returns `Context | None` and every consumer (T6, T7, T8) passes it straight into `context=`.
