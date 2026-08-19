"""Usage generations: the Langfuse attribute contract (design §3.1, §5), the
`metrics_collected` listener that emits them, and its trace parenting (design §3.5)."""

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from livekit import rtc
from livekit.agents.metrics import STTMetrics, TTSMetrics
from livekit.agents.metrics.base import Metadata
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.correlation import CALL_TRACE_NAME, TRACE_NAME_ATTR
from vera_core.observability.otel_testing import assert_no_phi_values
from vera_core.observability.usage_spans import (
    GENERATION,
    OBSERVATION_MODEL_ATTR,
    OBSERVATION_TYPE_ATTR,
    SPAN_STT_USAGE,
    SPAN_TTS_USAGE,
    STT_AUDIO_MS,
    TTS_CHARACTERS,
    USAGE_DETAILS_ATTR,
    attach_usage_spans,
    usage_span_attributes,
)
from vera_core.stt import ResilientSTT, STTSpec


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
        "label": "cartesia",
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
        # Characters handed to the synthesizer are billed whether or not the request was
        # torn down, so they must still be reported. The cancelled FLAG itself is not
        # surfaced — see usage_span_attributes for why it carries no signal.
        attrs = usage_span_attributes(_tts(cancelled=True))
        assert attrs is not None
        assert _usage(attrs)[TTS_CHARACTERS] == 465
        assert "vera.usage.cancelled" not in attrs

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


_ROOM = "call--00000000-0000-0000-0000-0000000000aa--00000000-0000-0000-0000-0000000000bb"


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
        assert [span.name for span in otel_spans.get_finished_spans()] == [SPAN_STT_USAGE]

    def test_tts_event_emits_a_named_generation(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _tts())
        assert [span.name for span in otel_spans.get_finished_spans()] == [SPAN_TTS_USAGE]

    def test_zero_usage_event_emits_nothing(self, otel_spans: Any) -> None:
        emitter = _FakeEmitter()
        attach_usage_spans(emitter)
        emitter.emit("metrics_collected", _stt(audio_duration=0.0, request_id=""))
        assert otel_spans.get_finished_spans() == ()

    def test_room_name_adds_call_correlation(self, otel_spans: Any) -> None:
        call = "00000000-0000-0000-0000-0000000000bb"
        room = _ROOM
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, room_name=room)
        emitter.emit("metrics_collected", _stt())
        span = _only(otel_spans, SPAN_STT_USAGE)
        assert span.attributes["langfuse.session.id"] == room
        assert span.attributes["vera.call_id"] == call

    def test_a_usage_span_restates_the_call_trace_name(self, otel_spans: Any) -> None:
        """Carrying langfuse.session.id makes Langfuse re-derive the trace from this
        span, and that path takes the name from langfuse.trace.name alone — without it
        the hundreds of usage spans on a call blank the trace's name."""
        emitter = _FakeEmitter()
        attach_usage_spans(emitter, room_name=_ROOM)
        emitter.emit("metrics_collected", _stt())
        assert _only(otel_spans, SPAN_STT_USAGE).attributes[TRACE_NAME_ATTR] == CALL_TRACE_NAME

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


class TestResilientSTTChain:
    @pytest.mark.asyncio
    async def test_the_fallback_chain_emits_usage_generations(self, otel_spans: Any) -> None:
        """Whisper STT is real Deepgram spend. Attaching to the FallbackAdapter (not
        each inner STT) is deliberate: it re-emits the inner STTMetrics verbatim
        (stt/fallback_adapter.py:294), so the model name stays the true provider's
        rather than the literal 'FallbackAdapter'.

        Drives the real `_adapter()` lazy-build path (rather than hand-assigning
        `_chain`, which would skip straight past the code under test) with a stub
        `registry` and a patched `FallbackAdapter`, so this fails without a full
        fake of livekit's STT protocol (`test_stt.py`'s own documented reason for
        not covering FallbackAdapter integration there)."""
        chain = _FakeEmitter()  # stands in for the FallbackAdapter _adapter() builds

        def _fake_provider(spec: Any, secrets: Any, http_session: Any) -> Any:
            return object()  # never touched: FallbackAdapter itself is patched below

        stt = ResilientSTT(
            STTSpec("deepgram", "flux-general-en"), registry={"deepgram": _fake_provider}
        )
        with patch("livekit.agents.stt.FallbackAdapter", return_value=chain):
            built = stt._adapter()
        assert built is chain

        chain.emit("metrics_collected", _stt())
        assert (
            _only(otel_spans, SPAN_STT_USAGE).attributes[OBSERVATION_MODEL_ATTR]
            == "flux-general-en"
        )
