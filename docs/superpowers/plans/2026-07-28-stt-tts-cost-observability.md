# STT / TTS Usage & Cost Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Deepgram STT and Cartesia TTS usage and dollar cost visible per-turn in Langfuse traces, at every billed surface in Vera.

**Architecture:** One new pure-plus-wiring module in `vera_core.observability` emits a short Vera-owned span (`vera.stt.usage` / `vera.tts.usage`) per LiveKit `metrics_collected` event, carrying `langfuse.observation.usage_details` — a JSON attribute Langfuse parses with arbitrary usage keys, so non-token units (audio seconds, characters) get priced by Langfuse's own model-price mechanism. Vera holds no rates; a separate idempotent script seeds them into Langfuse via `POST /api/public/models`.

**Tech Stack:** Python 3.12, `livekit-agents 1.5.17`, `opentelemetry-sdk`, `httpx>=0.28`, self-hosted `langfuse/langfuse:3`, pytest, `just`.

**Spec:** `docs/superpowers/specs/2026-07-28-stt-tts-cost-observability-design.md` — read §5 (attributes), §7 (PHI guardrail) and §3.3 (trace parenting) before starting.

## Global Constraints

- **PHI:** never attach transcript text, `SpeechEvent.alternatives[0].text`, extracted answer values, or DTMF digits to any span or log line. Only counts, durations, booleans, closed enums, fixed model names, and the existing `call_trace_attributes` set are permitted (spec §7).
- **Never copy** the SDK's `metrics.model_dump_json()` blob onto a Vera span (spec §7 prohibition 1). Never attach `attribution.user_id` (prohibition 2).
- **Exception logging:** log `type(exc).__name__` only — never the exception repr or traceback (a provider error can embed the request payload). Never a bare `except` that would swallow `asyncio.CancelledError`.
- **Tracing must never break the call:** every attach and every span emit is wrapped in `try/except Exception` -> `logger.warning`.
- **Style:** PEP 695 type params (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`. `asyncio` only; never `import anyio`.
- **Gate:** `just check` (= ruff check + ruff format --check + mypy --strict + pytest) must pass. Run it verbatim, never a hand-picked subset.
- **After implementation, before the final commit:** run the `/simplify` skill on the change, then re-run `just check` on the exact tree being committed (repo `CLAUDE.md`).
- All backend paths below are relative to `vera-backend/`; the two doc paths in Task 7 are relative to the repo root.

## File Structure

| File | Responsibility |
|---|---|
| `packages/vera_core/src/vera_core/observability/usage_spans.py` | **New.** Pure attribute builder + the `metrics_collected` listener that emits usage spans. The only file that knows the Langfuse attribute contract. |
| `packages/vera_core/src/vera_core/observability/__init__.py` | Modify — re-export the two public functions. |
| `apps/agent_worker/src/agent_worker/cascade.py` | Modify `build_session` — bind `stt`/`tts` to locals and attach listeners. |
| `apps/agent_worker/src/agent_worker/main.py` | Modify — capture the OTel context once in the entrypoint; pass it to `build_session`; replace the takeover `stt_factory` lambda with a function that attaches per-track. |
| `apps/agent_worker/src/agent_worker/takeover_transcript.py` | Modify — widen `stt_factory` to receive `SpeakerAttribution` so the span can carry `vera.usage.source`. |
| `packages/vera_core/src/vera_core/stt.py` | Modify `_adapter()` — attach one listener to the `FallbackAdapter` chain. |
| `apps/control_plane/src/control_plane/api/v1/coaching.py` | Modify — wrap the whisper `transcribe()` in a `vera.coaching.whisper` parent span carrying the call's session id. |
| `scripts/seed_langfuse_prices.py` | **New.** Idempotent Langfuse model-price seeder. |
| `justfile` | Modify — add the `langfuse-seed-prices` recipe. |
| `adr/devops-todo.md` | Modify — add row 22. |
| `docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md` | **New.** Manual price-entry fallback (repo root). |

**Tests:**

| File | Covers |
|---|---|
| `tests/unit/observability/test_usage_spans.py` | **New.** Tasks 1-2: attribute shape, zero-usage skip, cancelled TTS, PHI denylist, trace parenting. |
| `apps/agent_worker/tests/unit/test_cascade.py` | Extend — Task 3 cascade wiring. |
| `tests/unit/agent_worker/test_takeover_transcript.py` | Extend — Task 4 per-track factory + `vera.usage.source`. |
| `tests/unit/observability/test_seed_langfuse_prices.py` | **New.** Task 6 seeder idempotency + refuse-to-seed-zero. |

---

### Task 1: The pure attribute builder

Builds the exact span attributes for one metrics event. No OTel, no I/O, no wiring — so the part Langfuse actually contracts on is testable in isolation.

**Files:**
- Create: `packages/vera_core/src/vera_core/observability/usage_spans.py`
- Test: `tests/unit/observability/test_usage_spans.py`

**Interfaces:**
- Consumes: `livekit.agents.metrics.STTMetrics` / `TTSMetrics` (fields per spec §2.4).
- Produces:
  - `usage_span_attributes(metrics: Any) -> dict[str, str | int | float | bool] | None`
  - Constants `SPAN_STT_USAGE = "vera.stt.usage"`, `SPAN_TTS_USAGE = "vera.tts.usage"`, `USAGE_DETAILS_ATTR = "langfuse.observation.usage_details"`, `STT_AUDIO_SECONDS = "stt_audio_seconds"`, `TTS_CHARACTERS = "tts_characters"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/test_usage_spans.py`:

```python
"""Usage-span attribute shape (design §5) — the Langfuse contract, tested pure."""

import json
from typing import Any

from livekit.agents.metrics import Metadata, STTMetrics, TTSMetrics

from vera_core.observability.usage_spans import (
    STT_AUDIO_SECONDS,
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
        "metadata": Metadata(model_name="sonic-3.5", model_provider="Cartesia"),
    }
    return TTSMetrics(**{**base, **over})


def _usage(attrs: dict[str, Any]) -> dict[str, float]:
    parsed: dict[str, float] = json.loads(attrs[USAGE_DETAILS_ATTR])
    return parsed


class TestSTTAttributes:
    def test_audio_seconds_is_the_only_usage_key(self) -> None:
        attrs = usage_span_attributes(_stt())
        assert attrs is not None
        assert _usage(attrs) == {STT_AUDIO_SECONDS: 27.64}

    def test_model_and_provider_come_from_metadata(self) -> None:
        attrs = usage_span_attributes(_stt())
        assert attrs is not None
        # Langfuse regex-matches its model price entry against gen_ai.request.model.
        assert attrs["gen_ai.request.model"] == "flux-general-en"
        assert attrs["gen_ai.provider.name"] == "Deepgram"

    def test_streamed_flag_is_carried(self) -> None:
        attrs = usage_span_attributes(_stt(streamed=True))
        assert attrs is not None
        assert attrs["vera.usage.streamed"] is True

    def test_connection_acquired_event_yields_no_span(self) -> None:
        # stt.py:369-383 _report_connection_acquired emits a real STTMetrics with zero
        # usage purely to report websocket connect timing (design §5.1). Emitting a span
        # for it would add a $0 noise span per connect.
        assert usage_span_attributes(_stt(audio_duration=0.0, request_id="")) is None

    def test_token_billed_stt_folds_tokens_in(self) -> None:
        attrs = usage_span_attributes(_stt(input_tokens=120, output_tokens=8))
        assert attrs is not None
        assert _usage(attrs) == {
            STT_AUDIO_SECONDS: 27.64,
            "input_tokens": 120,
            "output_tokens": 8,
        }

    def test_zero_tokens_are_omitted_not_sent_as_zero(self) -> None:
        # A zero-valued key would demand a Langfuse price entry for a unit nobody bills.
        assert "input_tokens" not in _usage(usage_span_attributes(_stt()) or {})


class TestTTSAttributes:
    def test_characters_is_the_only_usage_key(self) -> None:
        attrs = usage_span_attributes(_tts())
        assert attrs is not None
        assert _usage(attrs) == {TTS_CHARACTERS: 465}

    def test_audio_seconds_is_operational_not_billed(self) -> None:
        attrs = usage_span_attributes(_tts())
        assert attrs is not None
        assert attrs["vera.usage.audio_seconds"] == 27.64
        assert "audio_duration" not in _usage(attrs)

    def test_cancelled_tts_still_counts_its_characters(self) -> None:
        # Barge-in: those characters already went to Cartesia and are billed (design §5.2).
        attrs = usage_span_attributes(_tts(cancelled=True))
        assert attrs is not None
        assert _usage(attrs) == {TTS_CHARACTERS: 465}
        assert attrs["vera.usage.cancelled"] is True

    def test_empty_synthesis_yields_no_span(self) -> None:
        assert usage_span_attributes(_tts(characters_count=0)) is None


class TestDefensiveHandling:
    def test_unknown_metrics_type_yields_none(self) -> None:
        assert usage_span_attributes(object()) is None

    def test_missing_metadata_omits_model_attributes(self) -> None:
        attrs = usage_span_attributes(_stt(metadata=None))
        assert attrs is not None
        assert "gen_ai.request.model" not in attrs
        assert _usage(attrs) == {STT_AUDIO_SECONDS: 27.64}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'vera_core.observability.usage_spans'`

- [ ] **Step 3: Write the module**

Create `packages/vera_core/src/vera_core/observability/usage_spans.py`:

```python
"""Per-turn STT/TTS usage spans that Langfuse can price.

LiveKit reports STT usage ONLY through the OTel *Metrics* API (a no-op meter here,
since `otel.py` installs a TracerProvider and nothing else) and Langfuse ingests
traces only — so STT usage reaches Langfuse nowhere. TTS does get a span, but its
usage rides a custom `lk.tts_metrics` bag that Langfuse's cost engine does not read.

Both are fixed the same way: one short Vera-owned span per `metrics_collected`
event, carrying `langfuse.observation.usage_details`. Langfuse parses that
attribute with ARBITRARY usage keys and matches each key against a model price
entry, so non-token billing units (audio seconds, synthesized characters) price
exactly like LLM tokens do. Vera holds no rates — see the price seeder in
`scripts/seed_langfuse_prices.py`.

PHI: neither STTMetrics nor TTSMetrics carries any text field (`characters_count`
is `len(input_text)`, a length). Every attribute below is a count, duration,
boolean, closed enum, or a model name Vera itself passed in. Never add the SDK's
`metrics.model_dump_json()` blob here — it carries no PHI today but would attach
whatever fields a future SDK version adds, sight unseen.
"""

import json
import logging
from typing import Any

from livekit.agents.metrics import STTMetrics, TTSMetrics
from opentelemetry import trace
from opentelemetry.context import Context

from vera_core.observability.correlation import call_trace_attributes

logger = logging.getLogger("vera.observability")

SPAN_STT_USAGE = "vera.stt.usage"
SPAN_TTS_USAGE = "vera.tts.usage"

# The attribute Langfuse parses for usage. Keys inside are matched EXACTLY against
# the per-usage-type prices of the matched model definition, then summed.
USAGE_DETAILS_ATTR = "langfuse.observation.usage_details"

# Usage keys. These strings are a contract with the Langfuse model price entries the
# seeder writes — changing one here without changing it there silently zeroes cost.
STT_AUDIO_SECONDS = "stt_audio_seconds"
TTS_CHARACTERS = "tts_characters"

_tracer = trace.get_tracer("vera.observability.usage")

type _Attributes = dict[str, str | int | float | bool]


def _billable_tokens(metrics: STTMetrics | TTSMetrics) -> dict[str, float]:
    """Token counts, included only when non-zero: Deepgram and Cartesia both report 0,
    and a zero-valued key would demand a price entry for a unit nobody bills."""
    tokens: dict[str, float] = {}
    if metrics.input_tokens:
        tokens["input_tokens"] = metrics.input_tokens
    if metrics.output_tokens:
        tokens["output_tokens"] = metrics.output_tokens
    return tokens


def usage_span_attributes(metrics: Any) -> _Attributes | None:
    """Span attributes for one metrics event, or None when there is nothing billable.

    None covers two real cases: `_report_connection_acquired`'s zero-usage
    connection-timing event (`stt.py:369-383`), and any metrics type that is not
    STT/TTS (VAD/EOU cost nothing).
    """
    attrs: _Attributes
    if isinstance(metrics, TTSMetrics):
        usage: dict[str, float] = dict(_billable_tokens(metrics))
        if metrics.characters_count:
            usage[TTS_CHARACTERS] = metrics.characters_count
        if not usage:
            return None
        attrs = {
            USAGE_DETAILS_ATTR: json.dumps(usage),
            "vera.usage.streamed": metrics.streamed,
            "vera.usage.cancelled": metrics.cancelled,
            # Operational only — Cartesia bills characters, not output duration.
            "vera.usage.audio_seconds": metrics.audio_duration,
        }
    elif isinstance(metrics, STTMetrics):
        usage = dict(_billable_tokens(metrics))
        if metrics.audio_duration:
            usage[STT_AUDIO_SECONDS] = metrics.audio_duration
        if not usage:
            return None
        attrs = {
            USAGE_DETAILS_ATTR: json.dumps(usage),
            "vera.usage.streamed": metrics.streamed,
        }
    else:
        return None

    if (meta := metrics.metadata) is not None:
        # gen_ai.request.model is what Langfuse regex-matches to a model price entry.
        if meta.model_name:
            attrs["gen_ai.request.model"] = meta.model_name
        if meta.model_provider:
            attrs["gen_ai.provider.name"] = meta.model_provider
    return attrs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Run the full gate**

Run: `cd vera-backend && just check`
Expected: PASS. If mypy objects to `type _Attributes = ...` used before definition, move the alias above `_billable_tokens`.

- [ ] **Step 6: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/observability/usage_spans.py \
        vera-backend/tests/unit/observability/test_usage_spans.py
git commit -m "feat(otel): build Langfuse-priceable usage attributes from STT/TTS metrics"
```

---

### Task 2: The listener and trace parenting

Registers the `metrics_collected` listener and emits the span. The parenting test here is the load-bearing one — see spec §3.3.

**Files:**
- Modify: `packages/vera_core/src/vera_core/observability/usage_spans.py`
- Modify: `packages/vera_core/src/vera_core/observability/__init__.py`
- Test: `tests/unit/observability/test_usage_spans.py` (append)

**Interfaces:**
- Consumes: `usage_span_attributes` (Task 1); `call_trace_attributes` from `vera_core.observability.correlation`.
- Produces: `attach_usage_spans(emitter: Any, *, parent_context: Context | None = None, room_name: str | None = None, source: str | None = None) -> None`. Tasks 3-5 all call exactly this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/observability/test_usage_spans.py`:

```python
import asyncio

import pytest
from livekit import rtc
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import (
    assert_no_phi_values,
    install_test_tracer_provider,
)
from vera_core.observability.usage_spans import (
    SPAN_STT_USAGE,
    SPAN_TTS_USAGE,
    attach_usage_spans,
)


class _FakeEmitter(rtc.EventEmitter[str]):
    """Stands in for deepgram.STTv2 / cartesia.TTS / stt.FallbackAdapter — all of
    which are rtc.EventEmitters that emit 'metrics_collected'."""


@pytest.fixture
def spans() -> Any:
    exporter: InMemorySpanExporter = install_test_tracer_provider()
    exporter.clear()
    yield exporter
    exporter.clear()


def _only(exporter: InMemorySpanExporter, name: str) -> Any:
    return next(s for s in exporter.get_finished_spans() if s.name == name)


class TestListener:
    def test_stt_event_emits_a_named_span(self, spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt())
        assert _only(spans, SPAN_STT_USAGE) is not None

    def test_tts_event_emits_a_named_span(self, spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _tts())
        assert _only(spans, SPAN_TTS_USAGE) is not None

    def test_zero_usage_event_emits_nothing(self, spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt(audio_duration=0.0, request_id=""))
        assert spans.get_finished_spans() == ()

    def test_room_name_adds_call_correlation(self, spans: Any) -> None:
        tenant = "00000000-0000-0000-0000-0000000000aa"
        call = "00000000-0000-0000-0000-0000000000bb"
        room = f"call--{tenant}--{call}"
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, room_name=room)
        emitter.emit("metrics_collected", _stt())
        span = _only(spans, SPAN_STT_USAGE)
        assert span.attributes["langfuse.session.id"] == room
        assert span.attributes["vera.call_id"] == call

    def test_source_is_set_only_when_given(self, spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, source="supervisor")
        emitter.emit("metrics_collected", _stt())
        assert _only(spans, SPAN_STT_USAGE).attributes["vera.usage.source"] == "supervisor"

    def test_cascade_span_has_no_source(self, spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt())
        assert "vera.usage.source" not in _only(spans, SPAN_STT_USAGE).attributes

    def test_no_phi_reaches_the_span(self, spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _tts())
        # Substring check: an attribute merely EMBEDDING a transcript fails too.
        assert_no_phi_values(_only(spans, SPAN_TTS_USAGE), "Jane Doe", "member id 1234")

    def test_a_broken_event_never_propagates_to_the_caller(self, spans: Any) -> None:
        # A tracing failure must never break the call (design §8.1).
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", None)  # must not raise
        assert spans.get_finished_spans() == ()


class TestTraceParenting:
    """Design §3.3: the takeover STT is reached via a room-event callback whose task
    does NOT carry job_entrypoint's context. Without an explicitly captured parent,
    usage spans become NEW TRACE ROOTS — they fall out of the call's trace and never
    sum into its cost, and nothing else about the output looks wrong."""

    @pytest.mark.asyncio
    async def test_captured_context_survives_a_foreign_task(self, spans: Any) -> None:
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

        span = _only(spans, SPAN_STT_USAGE)
        assert span.parent is not None
        assert span.parent.span_id == parent_ctx.span_id
        assert span.context.trace_id == parent_ctx.trace_id

    @pytest.mark.asyncio
    async def test_without_a_captured_context_the_span_is_orphaned(self, spans: Any) -> None:
        """Documents exactly what §3.3 prevents, so the guarantee above is meaningful.

        LiveKit's room-event dispatch task is created when the room connects — BEFORE
        the per-track STT exists. A task created outside the parent span's lifetime
        carries none of its context, so emitting from there with no captured parent
        produces a NEW TRACE ROOT: the usage span leaves the call's trace and its cost
        never sums into the call total.
        """
        tracer = trace.get_tracer("test")
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)  # no parent_context — the bug

        # Created before any span exists, standing in for the room-dispatch task.
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
        assert _only(spans, SPAN_STT_USAGE).parent is None

    def test_ambient_context_is_used_when_no_parent_is_captured(self, spans: Any) -> None:
        # The coaching-whisper path relies on this: its parent span is per-request, so
        # it CANNOT be captured at attach time (design §3.4).
        tracer = trace.get_tracer("test")
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        with tracer.start_as_current_span("vera.coaching.whisper") as parent:
            emitter.emit("metrics_collected", _stt())
            expected = parent.get_span_context()
        span = _only(spans, SPAN_STT_USAGE)
        assert span.parent is not None
        assert span.parent.span_id == expected.span_id
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py -v -k "Listener or Parenting"`
Expected: FAIL with `ImportError: cannot import name 'attach_usage_spans'`

- [ ] **Step 3: Append the listener to the module**

Add to the end of `packages/vera_core/src/vera_core/observability/usage_spans.py`:

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
    # A point-in-time span: every attribute is known up front, so open and close it
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
    """Emit one usage span per billable `metrics_collected` event from *emitter*.

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
    the latter is deprecated in livekit-agents 1.5.17 and warns per registration.
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

- [ ] **Step 6: Run the full gate**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/observability/ \
        vera-backend/tests/unit/observability/test_usage_spans.py
git commit -m "feat(otel): emit per-turn STT/TTS usage spans with an explicit trace parent"
```

---

### Task 3: Wire the main cascade STT and TTS

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/cascade.py:111-135`
- Modify: `apps/agent_worker/src/agent_worker/main.py` (near `:342` and `:432`)
- Test: `apps/agent_worker/tests/unit/test_cascade.py` (extend)

**Interfaces:**
- Consumes: `attach_usage_spans` (Task 2).
- Produces: `build_session(..., room_name: str | None = None, parent_context: Context | None = None)`. Task 4 reuses the same `usage_parent_ctx` local that this task adds to `main.py`.

- [ ] **Step 1: Write the failing test**

Append to `apps/agent_worker/tests/unit/test_cascade.py`:

```python
class TestUsageSpanWiring:
    def test_cascade_stt_and_tts_both_emit_usage_spans(self, otel_spans: Any) -> None:
        """build_session must attach listeners to the STT and TTS it constructs —
        without it, Deepgram/Cartesia spend stays invisible in Langfuse."""
        from livekit.agents.metrics import Metadata, TTSMetrics

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
                metadata=Metadata(model_name="sonic-3.5", model_provider="Cartesia"),
            ),
        )
        span = next(s for s in otel_spans.get_finished_spans() if s.name == SPAN_TTS_USAGE)
        assert span.attributes["gen_ai.request.model"] == "sonic-3.5"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_cascade.py::TestUsageSpanWiring -v`
Expected: FAIL with `StopIteration` — no `vera.tts.usage` span was produced.

- [ ] **Step 3: Bind STT/TTS to locals and attach**

In `cascade.py`, add to the imports:

```python
from opentelemetry.context import Context

from vera_core.observability import attach_usage_spans
```

Replace `build_session` (`cascade.py:111-135`) with:

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
    tts = cartesia.TTS(model="sonic-3.5", emotion=["confident"])
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

- [ ] **Step 4: Capture the entrypoint context in main.py**

In `main.py`, add to the imports:

```python
from opentelemetry import context as otel_context
```

Immediately after the existing `trace.get_current_span().set_attributes(call_trace_attributes(room_name))` (`main.py:342`), insert:

```python
        # Captured HERE, where LiveKit's job_entrypoint span is genuinely ambient
        # (ipc/job_proc_lazy_main.py:316). Usage-span listeners are registered from
        # code that later runs in other tasks — notably the takeover STT, reached via
        # a room-event callback whose task does NOT carry this context. Capturing the
        # context now and closing over the value keeps every usage span inside this
        # call's trace; reading the ambient context at emit time would silently
        # produce new trace roots that never sum into the call's cost.
        usage_parent_ctx = otel_context.get_current()
```

Then pass it into the `build_session` call (`main.py:432-438`):

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

- [ ] **Step 6: Run the full gate**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/cascade.py \
        vera-backend/apps/agent_worker/src/agent_worker/main.py \
        vera-backend/apps/agent_worker/tests/unit/test_cascade.py
git commit -m "feat(otel): price the cascade's Deepgram STT and Cartesia TTS in Langfuse"
```

---

### Task 4: Wire the per-track takeover STT

A takeover transcribes two channels (the callee and an intervening supervisor) with a fresh STT per track, all billed and all invisible today. The factory is widened so each span can say which channel it billed.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/takeover_transcript.py:86-98,136-138`
- Modify: `apps/agent_worker/src/agent_worker/main.py:535-543`
- Test: `tests/unit/agent_worker/test_takeover_transcript.py` (extend)

**Interfaces:**
- Consumes: `attach_usage_spans` (Task 2); `usage_parent_ctx` (Task 3, same `main.py` scope).
- Produces: `TakeoverTranscriber(..., stt_factory: Callable[[SpeakerAttribution], agents_stt.STT[Any]], ...)` — a **breaking signature change**; the factory now receives the attribution.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/agent_worker/test_takeover_transcript.py`:

```python
class TestSTTFactoryAttribution:
    def test_factory_receives_the_attribution_for_the_track(self) -> None:
        """The span must be able to say WHICH channel it billed — a takeover runs two
        concurrent Deepgram streams (callee + intervening supervisor)."""
        from agent_worker.takeover_transcript import SpeakerAttribution

        seen: list[SpeakerAttribution] = []

        def factory(attribution: SpeakerAttribution) -> Any:
            seen.append(attribution)
            return _FakeSTT()

        # Reuse this module's existing harness for driving one subscribed track
        # through TakeoverTranscriber; assert the factory saw a real attribution.
        _drive_one_track(stt_factory=factory)
        assert [a.source for a in seen] == [SOURCE_REP]
```

Adapt `_FakeSTT` / `_drive_one_track` to whatever fakes the file already defines — read the existing tests first and reuse their harness rather than adding a parallel one. If the file has no such harness, build the minimal one: a `TakeoverTranscriber` with a stub `rtc.Room` whose `remote_participants` yields one audio publication for `callee_identity`, then call `.start()`.

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
        # spans (a takeover bills two concurrent STT streams).
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

In `main.py`, extend the existing takeover import (`:45`):

```python
from agent_worker.takeover_transcript import SpeakerAttribution, TakeoverTranscriber
```

Then replace the `TakeoverTranscriber(...)` construction (`main.py:535-543`) with:

```python
        takeover_transcriber: TakeoverTranscriber | None = None
        if turn_sink is not None and speaker is not None:

            def _takeover_stt(attribution: SpeakerAttribution) -> Any:
                # One STT per subscribed track, so one listener per track. The parent
                # context is the entrypoint's, NOT the ambient one: a supervisor who
                # joins after takeover starts arrives via room.on("track_subscribed"),
                # whose task does not carry the entrypoint span (design §3.3).
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

Add `attach_usage_spans` to `main.py`'s `vera_core.observability` import.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/agent_worker/test_takeover_transcript.py -v`
Expected: PASS (including the pre-existing tests — fix any that construct `TakeoverTranscriber` with a zero-arg factory by giving the lambda an ignored parameter)

- [ ] **Step 6: Run the full gate**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/takeover_transcript.py \
        vera-backend/apps/agent_worker/src/agent_worker/main.py \
        vera-backend/tests/unit/agent_worker/test_takeover_transcript.py
git commit -m "feat(otel): price the per-track takeover STT, tagged by channel"
```

---

### Task 5: Wire coaching's hold-to-whisper STT

The whisper chain runs in the **control plane**, so it cannot share the worker's trace (spec §3.4). It gets its own trace, correlated into the same Langfuse session by room name.

**Files:**
- Modify: `packages/vera_core/src/vera_core/stt.py:154-186`
- Modify: `apps/control_plane/src/control_plane/api/v1/coaching.py:189-197`
- Test: `tests/unit/observability/test_usage_spans.py` (append)

**Interfaces:**
- Consumes: `attach_usage_spans` (Task 2); `call_trace_attributes` / `room_name_for_call` from `vera_core.observability`.
- Produces: a `vera.coaching.whisper` span that is the trace root for a whisper request; `vera.stt.usage` nests under it via ambient context.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/observability/test_usage_spans.py`:

```python
class TestResilientSTTChain:
    def test_the_fallback_chain_emits_usage_spans(self, spans: Any) -> None:
        """Whisper STT is real Deepgram spend. Attaching to the FallbackAdapter (not
        each inner STT) is deliberate: it re-emits the inner STTMetrics verbatim, so
        the model name stays the true provider's rather than 'FallbackAdapter'."""
        from vera_core.stt import ResilientSTT, STTSpec

        chain = _FakeEmitter()
        stt = ResilientSTT(STTSpec("deepgram", "flux-general-en"))
        stt._chain = chain  # the lazily-built FallbackAdapter
        attach_usage_spans(chain)

        chain.emit("metrics_collected", _stt())
        assert _only(spans, SPAN_STT_USAGE).attributes["gen_ai.request.model"] == (
            "flux-general-en"
        )
```

If reaching into `_chain` reads as too invasive when you get there, instead assert via `_adapter()` with a stub `registry` that returns `_FakeEmitter()` instances — `ResilientSTT.__init__` already accepts a `registry` override for exactly this kind of test.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_usage_spans.py::TestResilientSTTChain -v`
Expected: FAIL — no span produced (nothing attaches inside `_adapter()` yet), so `StopIteration`.

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
            # vera.coaching.whisper span (api/v1/coaching.py), which does not exist
            # yet at chain-construction time. Ambient context at emit time is correct
            # here — the metrics task is created inside transcribe(), so it inherits
            # the request's span. aclose() drops the chain, so a rebuilt chain gets a
            # fresh listener and there is no double registration.
            attach_usage_spans(self._chain)
        return self._chain
```

- [ ] **Step 4: Open the per-request parent span**

In `apps/control_plane/src/control_plane/api/v1/coaching.py`, add to the imports:

```python
from opentelemetry import trace

from vera_core.observability import call_trace_attributes, room_name_for_call
```

and near the module's other module-level constants:

```python
_tracer = trace.get_tracer("vera.control_plane.coaching")
```

Replace the transcribe block (`coaching.py:189-197`):

```python
    try:
        text = await whisper_stt.transcribe(
            audio_bytes, mime_type=audio.content_type or "audio/webm"
        )
    except STTUnavailableError as exc:
```

with:

```python
    try:
        # The trace root for this whisper request. It cannot join the agent worker's
        # trace — that job_entrypoint span lives in another process and no context is
        # propagated — so it carries langfuse.session.id instead, putting it in the
        # same Langfuse SESSION as the call it whispered into. The nested
        # vera.stt.usage span inherits this trace, which is what makes whisper spend
        # attributable to a call.
        #
        # record_exception/set_status_on_exception are OFF: an STT provider error can
        # embed the request payload (the supervisor's audio), and both of those would
        # copy its message onto the span.
        with _tracer.start_as_current_span(
            "vera.coaching.whisper",
            attributes=call_trace_attributes(room_name_for_call(tenant_id, call_id)),
            record_exception=False,
            set_status_on_exception=False,
        ):
            text = await whisper_stt.transcribe(
                audio_bytes, mime_type=audio.content_type or "audio/webm"
            )
    except STTUnavailableError as exc:
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/ tests/unit/control_plane/ -v`
Expected: PASS

- [ ] **Step 6: Run the full gate**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/stt.py \
        vera-backend/apps/control_plane/src/control_plane/api/v1/coaching.py \
        vera-backend/tests/unit/observability/test_usage_spans.py
git commit -m "feat(otel): price coaching's whisper STT, session-correlated to its call"
```

---

### Task 6: The Langfuse price seeder

Without a matching model price entry, the usage attributes ingest fine and every observation renders blank cost. This creates the entries.

**Files:**
- Create: `scripts/seed_langfuse_prices.py`
- Modify: `justfile` (after the `langfuse-down` recipe, ~line 49)
- Test: `tests/unit/observability/test_seed_langfuse_prices.py`

**Interfaces:**
- Consumes: `get_settings()` for `langfuse_host` / `langfuse_public_key` / `langfuse_secret_key`.
- Produces: `MODELS: tuple[ModelPrice, ...]`, `build_payload(price: ModelPrice, model_id: str | None) -> dict[str, Any]`, `resolve_rates(env: Mapping[str, str]) -> dict[str, float]`, `async def seed(client: httpx.AsyncClient) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/test_seed_langfuse_prices.py`:

```python
"""Seeder contract: idempotent upsert, and never seed a zero price."""

from typing import Any

import pytest

from scripts.seed_langfuse_prices import (
    MODELS,
    MissingRateError,
    build_payload,
    resolve_rates,
)


class TestRates:
    def test_all_three_models_resolve_from_env(self) -> None:
        rates = resolve_rates(
            {
                "LANGFUSE_PRICE_STT_FLUX_PER_SECOND": "0.000128",
                "LANGFUSE_PRICE_STT_NOVA_PER_SECOND": "0.000128",
                "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER": "0.000022",
            }
        )
        assert set(rates) == {m.env_var for m in MODELS}
        assert rates["LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER"] == 0.000022

    def test_a_missing_rate_refuses_to_seed(self) -> None:
        # A $0.00 entry is indistinguishable from broken instrumentation in the UI, so
        # a partial seed is worse than no seed (design §6.1).
        with pytest.raises(MissingRateError):
            resolve_rates({"LANGFUSE_PRICE_STT_FLUX_PER_SECOND": "0.000128"})

    def test_an_unparseable_rate_refuses_to_seed(self) -> None:
        with pytest.raises(MissingRateError):
            resolve_rates(
                {
                    "LANGFUSE_PRICE_STT_FLUX_PER_SECOND": "cheap",
                    "LANGFUSE_PRICE_STT_NOVA_PER_SECOND": "0.000128",
                    "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER": "0.000022",
                }
            )


class TestPayload:
    def test_a_new_model_carries_no_model_id(self) -> None:
        payload = build_payload(MODELS[0], None, rate=0.000128)
        assert "modelId" not in payload
        assert payload["modelName"] == MODELS[0].model_name
        assert payload["matchPattern"] == MODELS[0].match_pattern

    def test_an_existing_model_threads_its_id_back_in(self) -> None:
        # POST /api/public/models upserts ONLY when given an existing modelId; a
        # duplicate modelName without one is rejected on (projectId, modelName).
        payload = build_payload(MODELS[0], "clx123", rate=0.000128)
        assert payload["modelId"] == "clx123"

    def test_price_uses_the_usage_key_the_instrumentation_sends(self) -> None:
        payload = build_payload(MODELS[0], None, rate=0.000128)
        prices: dict[str, Any] = payload["pricingTiers"][0]["prices"]
        assert prices == {MODELS[0].usage_key: 0.000128}

    def test_patterns_match_the_model_families_vera_actually_uses(self) -> None:
        import re

        by_key = {m.model_name: m for m in MODELS}
        assert re.match(by_key["vera-deepgram-flux"].match_pattern, "flux-general-en")
        assert re.match(by_key["vera-deepgram-nova"].match_pattern, "nova-3")
        assert re.match(by_key["vera-cartesia-sonic"].match_pattern, "sonic-3.5")

    def test_patterns_survive_a_model_version_bump(self) -> None:
        # Family patterns, not exact versions: an exact pattern would silently zero
        # cost on the next bump, and a missing match looks identical to "no data".
        import re

        by_key = {m.model_name: m for m in MODELS}
        assert re.match(by_key["vera-cartesia-sonic"].match_pattern, "sonic-4")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_seed_langfuse_prices.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.seed_langfuse_prices'`

- [ ] **Step 3: Write the script**

Create `scripts/seed_langfuse_prices.py`:

```python
"""Seed the Langfuse custom model price entries for Deepgram STT and Cartesia TTS.

Vera's usage spans carry raw usage only (`langfuse.observation.usage_details`, see
`vera_core/observability/usage_spans.py`); Langfuse does the arithmetic. It can only
do so if a model definition exists whose per-usage-type price keys match the usage
keys we send — otherwise usage ingests fine and every observation renders BLANK
cost, which looks exactly like broken instrumentation.

Rates are read from the environment, never hardcoded and deliberately NOT in
Settings: the application never needs a price, so keeping them here means there is
exactly one place prices live and no second copy inside Vera to drift.

    just langfuse-seed-prices

    LANGFUSE_PRICE_STT_FLUX_PER_SECOND       Deepgram Flux, $ per second of audio
    LANGFUSE_PRICE_STT_NOVA_PER_SECOND       Deepgram Nova, $ per second of audio
    LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER   Cartesia Sonic, $ per character

Public list prices are ~$0.0077/min for Deepgram (~$0.000128/second) and $5-37 per
million Cartesia characters — a SANITY REFERENCE ONLY. Use your contracted rates.

Idempotent: `POST /api/public/models` upserts only when handed an existing
`modelId`, and rejects a duplicate `modelName` without one, so this GETs the model
list first and threads any existing id back in. Re-running is a no-op-shaped update.

Writes to whatever VERA_LANGFUSE_HOST resolves to — the target host is logged.
"""

import asyncio
import base64
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from vera_core.config import get_settings
from vera_core.observability.usage_spans import STT_AUDIO_SECONDS, TTS_CHARACTERS

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
    # Must equal the key the instrumentation puts in usage_details.
    usage_key: str
    env_var: str


MODELS: tuple[ModelPrice, ...] = (
    ModelPrice(
        model_name="vera-deepgram-flux",
        match_pattern="(?i)^flux-.*$",
        usage_key=STT_AUDIO_SECONDS,
        env_var="LANGFUSE_PRICE_STT_FLUX_PER_SECOND",
    ),
    ModelPrice(
        model_name="vera-deepgram-nova",
        match_pattern="(?i)^nova-.*$",
        usage_key=STT_AUDIO_SECONDS,
        env_var="LANGFUSE_PRICE_STT_NOVA_PER_SECOND",
    ),
    ModelPrice(
        model_name="vera-cartesia-sonic",
        match_pattern="(?i)^sonic-.*$",
        usage_key=TTS_CHARACTERS,
        env_var="LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER",
    ),
)


def resolve_rates(env: Mapping[str, str]) -> dict[str, float]:
    """Every rate, or MissingRateError. All-or-nothing on purpose: a partial seed
    leaves some models priced and others blank, the hardest failure to spot."""
    rates: dict[str, float] = {}
    missing: list[str] = []
    for model in MODELS:
        raw = env.get(model.env_var)
        if raw is None or not raw.strip():
            missing.append(model.env_var)
            continue
        try:
            rate = float(raw)
        except ValueError:
            missing.append(f"{model.env_var} (not a number: {raw!r})")
            continue
        if rate <= 0:
            missing.append(f"{model.env_var} (must be > 0, got {rate})")
            continue
        rates[model.env_var] = rate
    if missing:
        raise MissingRateError("unusable rate env vars: " + ", ".join(missing))
    return rates


def build_payload(price: ModelPrice, model_id: str | None, *, rate: float) -> dict[str, Any]:
    """A CreateModelRequest body. `pricingTiers` (not the deprecated flat
    inputPrice/outputPrice) is the only shape that can express a custom usage key."""
    payload: dict[str, Any] = {
        "modelName": price.model_name,
        "matchPattern": price.match_pattern,
        "pricingTiers": [
            {
                "name": "default",
                "isDefault": True,
                "priority": 0,
                "conditions": [],
                "prices": {price.usage_key: rate},
            }
        ],
    }
    if model_id is not None:
        payload["modelId"] = model_id
    return payload


async def _existing_model_ids(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/public/models", params={"limit": 100})
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return {
        item["modelName"]: item["id"]
        for item in data.get("data", [])
        if item.get("modelName") and item.get("id")
    }


async def seed(client: httpx.AsyncClient, rates: dict[str, float]) -> list[str]:
    """Upsert every entry; returns the modelName/matchPattern pairs written, logged so
    a reader can compare them against the configured STT/TTS selectors."""
    existing = await _existing_model_ids(client)
    written: list[str] = []
    for price in MODELS:
        payload = build_payload(price, existing.get(price.model_name), rate=rates[price.env_var])
        response = await client.post("/api/public/models", json=payload)
        response.raise_for_status()
        written.append(f"{price.model_name} -> {price.match_pattern} [{price.usage_key}]")
    return written


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if not settings.langfuse_host:
        logger.error("VERA_LANGFUSE_HOST is unset — nothing to seed")
        return 1
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.error("VERA_LANGFUSE_PUBLIC_KEY / VERA_LANGFUSE_SECRET_KEY must both be set")
        return 1
    try:
        rates = resolve_rates(os.environ)
    except MissingRateError as exc:
        logger.error("%s", exc)
        logger.error("refusing to seed: a $0 price looks identical to broken instrumentation")
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
        for line in await seed(client, rates):
            logger.info("  %s", line)
    logger.info("done — verify under Settings -> Models")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: Add the `just` recipe**

In `justfile`, after the `langfuse-down` recipe (~line 49), add:

```
# Seed the Langfuse custom model prices so STT/TTS usage spans render a $ cost.
# Idempotent; refuses to run if any rate is unset. Rates are your CONTRACTED prices —
# public list is ~$0.0077/min Deepgram, $5-37/M chars Cartesia (sanity reference only).
langfuse-seed-prices:
    uv run python scripts/seed_langfuse_prices.py
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_seed_langfuse_prices.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Run the full gate**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/scripts/seed_langfuse_prices.py vera-backend/justfile \
        vera-backend/tests/unit/observability/test_seed_langfuse_prices.py
git commit -m "feat(otel): idempotent seeder for the Langfuse STT/TTS model prices"
```

---

### Task 7: Manual runbook and devops-todo row

**Files:**
- Create: `docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md` (repo root)
- Modify: `vera-backend/adr/devops-todo.md`

**Interfaces:**
- Consumes: the `MODELS` table from Task 6 (names, patterns, usage keys must match exactly).
- Produces: documentation only; no code depends on this task.

- [ ] **Step 1: Write the runbook**

Create `docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md` containing, in order:

1. **A pointer up front** that `just langfuse-seed-prices` is the preferred path and this doc is the fallback — so it does not quietly become the primary route and drift from the script.
2. **When to use it:** no shell access to an environment, adjusting one rate in the UI, or a Langfuse project stood up by hand.
3. **The click path:** Langfuse -> your project -> **Settings -> Models -> + New model**, with the UI-to-API field mapping (the UI says "match pattern" and "price"; the API calls them `matchPattern` and `pricingTiers[0].prices`).
4. **The fill-in table** — copy the values verbatim from `scripts/seed_langfuse_prices.py`'s `MODELS`:

   | modelName | match pattern | usage key | price |
   |---|---|---|---|
   | `vera-deepgram-flux` | `(?i)^flux-.*$` | `stt_audio_seconds` | $ per second |
   | `vera-deepgram-nova` | `(?i)^nova-.*$` | `stt_audio_seconds` | $ per second |
   | `vera-cartesia-sonic` | `(?i)^sonic-.*$` | `tts_characters` | $ per character |

5. **How to discover the usage key from a live span** instead of trusting this table: open any `vera.tts.usage` observation in a trace and read the keys off its `usage_details`. This is what keeps the runbook self-correcting if the attribute names ever change.
6. **A "cost is blank — why" triage table**, because all five causes look identical in the UI:

   | Cause | How to tell |
   |---|---|
   | No model entry at all | Settings -> Models has no `vera-*` row |
   | `matchPattern` does not match | Compare the pattern against the span's `gen_ai.request.model` |
   | Price key != usage key | Compare the entry's price key against the span's `usage_details` keys |
   | Usage key typo'd in instrumentation | `usage_details` shows an unexpected key name |
   | An unseeded model family | Cost renders on some usage spans but not others |

7. **The two warnings**, stated where a human will hit them: a `$0` entry is indistinguishable from broken instrumentation, and an exact-version pattern (`^sonic-3\.5$`) silently zeroes cost on the next model bump.
8. **Public list prices as a sanity reference only** — ~$0.0077/min Deepgram, $5-37 per million Cartesia characters — explicitly flagged as *not* the contracted rate.

- [ ] **Step 2: Add devops-todo row 22**

In `vera-backend/adr/devops-todo.md`, append this row to the table (after row 21, before the `## How to use this file` heading):

```
| 22 | ☐ **Seed the Langfuse custom model price entries in every environment** — `just langfuse-seed-prices` with `LANGFUSE_PRICE_STT_FLUX_PER_SECOND`, `LANGFUSE_PRICE_STT_NOVA_PER_SECOND` and `LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER` set, creating `vera-deepgram-flux`, `vera-deepgram-nova` and `vera-cartesia-sonic` with the real contracted rates. Re-run after any Langfuse project re-provision (the entries live in Langfuse's own DB, not in this repo) and after any STT/TTS model-family change. Manual fallback: `docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md`. | STT/TTS usage attributes ingest fine without a price entry, but every observation then renders blank cost — so runaway spend and cost regressions stay invisible, and a missing entry is indistinguishable in the UI from broken instrumentation. The rates are contract-specific rather than public list price, so they cannot ship in code; they are not secrets, just values that must exist wherever the seeder runs. | STT/TTS usage & cost observability (2026-07-28); spec `docs/superpowers/specs/2026-07-28-stt-tts-cost-observability-design.md`. |
```

- [ ] **Step 3: Verify the docs are consistent with the code**

Run: `cd vera-backend && grep -o 'vera-[a-z-]*' adr/devops-todo.md | sort -u && grep -o '"vera-[a-z-]*"' scripts/seed_langfuse_prices.py | sort -u`
Expected: the same three model names in both. Fix any mismatch.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-28-langfuse-price-entry-runbook.md \
        vera-backend/adr/devops-todo.md
git commit -m "docs: manual Langfuse price-entry runbook + devops-todo row 22"
```

---

### Task 8: Simplify, gate, and verify live

Unit tests prove the attributes are on the span. Only a real call proves Langfuse's side of the contract — which is the exact class of assumption that produced this bug.

**Files:**
- Modify: whatever `/simplify` flags across Tasks 1-7.

**Interfaces:**
- Consumes: everything above.
- Produces: a verified, committed change.

- [ ] **Step 1: Run the simplifier**

Invoke the `/simplify` skill on the full change (`git diff main...HEAD`). Required by repo `CLAUDE.md` before claiming done. Quality only — it does not hunt bugs.

- [ ] **Step 2: Re-run the full gate on the exact tree**

Run: `cd vera-backend && just check`
Expected: PASS. Must be run **after** the simplifier, on the tree being committed.

- [ ] **Step 3: Bring up Langfuse and seed the prices**

```bash
cd vera-backend
just langfuse-up
export LANGFUSE_PRICE_STT_FLUX_PER_SECOND=0.000128
export LANGFUSE_PRICE_STT_NOVA_PER_SECOND=0.000128
export LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER=0.000022
just langfuse-seed-prices
```

Expected: three `modelName -> pattern [usage_key]` lines. Confirm all three appear at http://localhost:4000 under Settings -> Models. Credentials are in `docker-compose.yml` (`LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`), project `proj-vera-local`.

- [ ] **Step 4: Boot the stack and exercise all four attach sites**

```bash
just up
just api      # separate terminal
just worker   # separate terminal
```

Then in one browser session: place a voice-lab test call (the `caller-` browser participant path — no telephony needed), **join as supervisor and click Intervene** so the dual-channel takeover STT runs, and use **hold-to-whisper** once so the coaching chain emits.

- [ ] **Step 5: Verify cost in the trace**

Open the new trace in Langfuse and confirm all four:

- [ ] **Every** `vera.stt.usage` and `vera.tts.usage` observation shows a non-blank `$` — check all of them, not just the cascade's: an unseeded model family shows up on some spans and not others.
- [ ] Takeover spans appear with both `vera.usage.source="rep"` and `="supervisor"`.
- [ ] All worker usage spans sit **inside the call's trace**, not as separate roots (this is the §3.3 guarantee holding in production conditions).
- [ ] The whisper span appears as its **own trace** but under the same `langfuse.session.id` — same Langfuse session view as the call.

- [ ] **Step 6: Check the arithmetic by hand**

Pick one `vera.tts.usage` span. Confirm `characters_count x LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER` matches the displayed cost. Repeat for one STT span with the per-second rate.

This is the only step that catches a seconds-vs-minutes unit mismatch, which would otherwise render a perfectly plausible number that is off by 60x — and it confirms the float-seconds decision (spec §5.3) survives Langfuse's ingestion rather than being silently truncated to an integer.

- [ ] **Step 7: Tear down and commit any simplifier changes**

```bash
just langfuse-down
git add -A && git commit -m "refactor: simplifier pass on the STT/TTS cost instrumentation"
```

(Skip the commit if the simplifier changed nothing.)

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3.1 new module, pure/wiring split | 1, 2 |
| §3.2 cascade STT + TTS attach sites | 3 |
| §3.2 takeover per-track attach site | 4 |
| §3.2 `ResilientSTT` chain attach site | 5 |
| §3.3 explicit parent context + regression test | 2 (test), 3 (capture), 4 (use) |
| §3.4 coaching whisper as its own session-correlated trace | 5 |
| §3.5 dual-channel `vera.usage.source` | 4 |
| §5 attribute shape, `gen_ai.request.model`, token folding | 1 |
| §5.1 zero-usage skip | 1 |
| §5.2 cancelled TTS still counts | 1 |
| §5.3 float seconds | 1 (unit), 8 step 6 (confirmed live) |
| §6.1 seeder: auth, env rates, family patterns, idempotency, refuse-zero | 6 |
| §6.1 unseeded-model-family mitigations (log written pairs, check every span) | 6 (`seed()` returns/logs), 8 step 5 |
| §6.2 manual runbook | 7 |
| §6.3 devops-todo row 22 | 7 |
| §7 PHI guardrail | 1 (module docstring), 2 (`assert_no_phi_values` test), Global Constraints |
| §8.1 error handling | 2 |
| §8.2 automated gate | 1, 2, 3, 4, 6 |
| §8.3 live verification | 8 |

No gaps.

**Type consistency:** `attach_usage_spans(emitter, *, parent_context, room_name, source)` is defined once in Task 2 and called with that exact keyword set in Tasks 3, 4 and 5. `usage_span_attributes` returns `dict | None` in Task 1 and every caller in Task 2 checks for `None`. `STT_AUDIO_SECONDS` / `TTS_CHARACTERS` are defined in Task 1 and imported by both the Task 6 script and its tests, so the usage-key-to-price-key contract cannot drift. `ModelPrice.env_var` keys the dict returned by `resolve_rates` and is the lookup used in `seed()`. `build_payload(price, model_id, *, rate)` — `rate` is keyword-only in both the definition and all four test call sites.

**Two deliberate deviations flagged for the implementer:**

1. Task 4 changes `TakeoverTranscriber.stt_factory` from `Callable[[], ...]` to `Callable[[SpeakerAttribution], ...]`. Pre-existing tests that pass a zero-arg factory will break and must be updated — expected, not a surprise.
2. Task 5's test reaches into `ResilientSTT._chain`. The step names the cleaner alternative (the existing `registry` constructor override) if that reads as too invasive on contact with the real file.
