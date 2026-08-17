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
