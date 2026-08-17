import json
from typing import Any

import pytest
from livekit.agents.metrics import STTMetrics, TTSMetrics
from livekit.agents.metrics.base import Metadata

from agent_worker.cascade import (
    build_speech_components,
    cascade_session_kwargs,
    llm_trace_attributes,
    resolve_llm_model,
    resolve_thinking_attrs,
    resolve_thinking_config,
)
from vera_core.observability.usage_spans import SPAN_STT_USAGE, SPAN_TTS_USAGE


def test_cascade_uses_turn_handling_only() -> None:
    # turn_handling is mutually exclusive with the deprecated flat kwargs: if it's set,
    # livekit-agents ignores every flat kwarg (incl. turn_detection). Exactly one key
    # here guarantees none of those silently leaked back in.
    kw = cascade_session_kwargs(turn_detector=object())
    assert set(kw) == {"turn_handling"}


def test_interruption_pinned_to_local_vad() -> None:
    # Self-hosted LiveKit OSS has no Cloud inference gateway; the adaptive (ML)
    # detector would 401 against agent-gateway.livekit.cloud and stream audio off-box.
    # Pin local VAD barge-in instead.
    interruption = cascade_session_kwargs(turn_detector=object())["turn_handling"]["interruption"]
    assert interruption["mode"] == "vad"
    assert interruption["min_duration"] == 0.5
    assert interruption["false_interruption_timeout"] == 2.0
    assert interruption["resume_false_interruption"] is True


def test_turn_detection_lives_inside_turn_handling() -> None:
    detector = object()
    turn_handling = cascade_session_kwargs(turn_detector=detector)["turn_handling"]
    # Must be nested here — a top-level turn_detection kwarg is dropped when turn_handling is set.
    assert turn_handling["turn_detection"] is detector


def test_cascade_latency_knobs() -> None:
    turn_handling = cascade_session_kwargs(turn_detector=object())["turn_handling"]
    assert turn_handling["endpointing"]["min_delay"] == 0.3
    assert turn_handling["endpointing"]["max_delay"] == 0.6
    assert turn_handling["preemptive_generation"]["enabled"] is True


def test_stt_kwargs_carry_plan_key_terms() -> None:
    # The CallPlan's stt_key_terms feed Deepgram keyterm prompting verbatim.
    from agent_worker.cascade import stt_kwargs

    assert stt_kwargs(["Cigna", "copay"]) == {"keyterm": ["Cigna", "copay"]}


def test_stt_kwargs_empty_without_key_terms() -> None:
    from agent_worker.cascade import stt_kwargs

    assert stt_kwargs(None) == {}
    assert stt_kwargs([]) == {}


def test_resolve_llm_model_uses_override_when_set() -> None:
    assert resolve_llm_model("gemini-3.5-flash", "gemini-2.5-flash") == "gemini-3.5-flash"


def test_resolve_llm_model_falls_back_to_default_when_unset() -> None:
    assert resolve_llm_model(None, "gemini-2.5-flash") == "gemini-2.5-flash"


def test_resolve_llm_model_falls_back_on_empty_string() -> None:
    assert resolve_llm_model("", "gemini-2.5-flash") == "gemini-2.5-flash"


def test_resolve_llm_model_default_is_the_caller_supplied_value_not_a_constant() -> None:
    # The fallback comes from Settings.voice_llm_default_model at the call site, not a
    # module constant — this pins that resolve_llm_model is a pure pass-through.
    assert resolve_llm_model(None, "some-other-model") == "some-other-model"


def test_resolve_thinking_attrs_honors_an_explicit_override() -> None:
    assert resolve_thinking_attrs("gemini-2.5-flash", {"thinking_budget": 500}) == {
        "thinking_budget": 500
    }
    assert resolve_thinking_attrs("gemini-3.5-flash", {"thinking_level": "high"}) == {
        "thinking_level": "high"
    }


def test_resolve_thinking_attrs_default_for_gemini_3_without_override() -> None:
    assert resolve_thinking_attrs("gemini-3.5-flash", None) == {"thinking_level": "low"}


def test_resolve_thinking_attrs_default_for_pre_3_without_override() -> None:
    assert resolve_thinking_attrs("gemini-2.5-flash", None) == {"thinking_budget": 0}


def test_resolve_thinking_attrs_falls_back_when_override_family_mismatches_model() -> None:
    # A stored override should always be paired with the model it was saved against
    # (save_llm_model/validate_extra_config enforce that), but this is the one place
    # that has the resolved model in hand — it must not trust the pairing blindly,
    # since a mismatch reaches google.LLM.chat() as a mid-call ValueError, not a clean
    # setup failure.
    assert resolve_thinking_attrs("gemini-3.5-flash", {"thinking_budget": 500}) == {
        "thinking_level": "low"
    }
    assert resolve_thinking_attrs("gemini-2.5-flash", {"thinking_level": "high"}) == {
        "thinking_budget": 0
    }


def test_resolve_thinking_attrs_drops_a_stray_extra_key() -> None:
    # extra_config is unconstrained JSONB (validated only on the save path) and
    # ThinkingConfig is extra="forbid", so a stray key passed through would raise at
    # session setup and drop the call. Only the family key may reach ThinkingConfig.
    assert resolve_thinking_attrs("gemini-2.5-flash", {"thinking_budget": 500, "junk": 1}) == {
        "thinking_budget": 500
    }
    assert resolve_thinking_attrs("gemini-3.5-flash", {"thinking_level": "high", "junk": 1}) == {
        "thinking_level": "high"
    }


def test_resolve_thinking_attrs_falls_back_when_no_family_key_survives() -> None:
    assert resolve_thinking_attrs("gemini-2.5-flash", {"junk": 1}) == {"thinking_budget": 0}


def test_resolve_thinking_attrs_falls_back_on_an_out_of_enum_thinking_level() -> None:
    # google.genai only WARNS on an unknown ThinkingLevel and ships it to the API, so a
    # bad stored level has to be caught here or not at all.
    assert resolve_thinking_attrs("gemini-3.5-flash", {"thinking_level": "sideways"}) == {
        "thinking_level": "low"
    }


def test_resolve_thinking_attrs_falls_back_on_an_uncoercible_thinking_budget() -> None:
    # ThinkingOverride is pydantic in lax mode, so "500" coerces to 500 — fine, it still
    # reaches ThinkingConfig as an int. Only a value no int can be made of falls back.
    for bad in (1.5, "abc", None, [0]):
        assert resolve_thinking_attrs("gemini-2.5-flash", {"thinking_budget": bad}) == {
            "thinking_budget": 0
        }


def test_resolve_thinking_config_never_raises_on_a_malformed_stored_override() -> None:
    # The point of re-validating: every one of these reaches ThinkingConfig as a valid
    # config instead of the ValidationError that would kill the call at session setup.
    for model, override in (
        ("gemini-2.5-flash", {"thinking_budget": 0, "junk": 1}),
        ("gemini-3.5-flash", {"thinking_level": "sideways"}),
        ("gemini-2.5-flash", {"thinking_budget": "abc"}),
        ("gemini-3.5-flash", {}),
    ):
        resolve_thinking_config(model, override)


def test_resolve_thinking_config_falls_back_instead_of_raising_on_mismatch() -> None:
    # google.LLM's own ThinkingConfig validation raises ValueError for a thinking_level
    # on a pre-3 model — resolve_thinking_config must never reach it with a mismatched
    # pairing.
    cfg = resolve_thinking_config("gemini-2.5-flash", {"thinking_level": "high"})
    assert cfg.thinking_budget == 0
    assert cfg.thinking_level is None


def test_resolve_thinking_config_builds_a_real_thinking_config_object() -> None:
    cfg = resolve_thinking_config("gemini-3.5-flash", {"thinking_level": "high"})
    assert cfg.thinking_budget is None
    assert cfg.thinking_level is not None

    cfg2 = resolve_thinking_config("gemini-2.5-flash", None)
    assert cfg2.thinking_budget == 0
    assert cfg2.thinking_level is None

    # The no-override Gemini-3 default path resolves to thinking_level="low".
    cfg3 = resolve_thinking_config("gemini-3.5-flash", None)
    assert cfg3.thinking_budget is None
    assert cfg3.thinking_level is not None


def test_llm_trace_attributes_prefixes_vera_llm() -> None:
    attrs = llm_trace_attributes("gemini-3.5-flash", {"thinking_level": "low"})
    assert attrs == {"vera.llm.model": "gemini-3.5-flash", "vera.llm.thinking_level": "low"}


def _stt_metrics() -> STTMetrics:
    return STTMetrics(
        request_id="r",
        timestamp=1.0,
        duration=0.0,
        label="deepgram.STTv2",
        audio_duration=5.0,
        streamed=True,
        metadata=Metadata(model_name="flux-general-en", model_provider="Deepgram"),
    )


def _tts_metrics() -> TTSMetrics:
    return TTSMetrics(
        request_id="r",
        timestamp=1.0,
        label="cartesia.TTS",  # required by the pydantic model
        ttfb=0.1,
        duration=1.0,
        audio_duration=2.0,
        cancelled=False,
        characters_count=42,
        streamed=True,
        metadata=Metadata(model_name="sonic-3.5-2026-05-04", model_provider="Cartesia"),
    )


def _span(otel_spans: Any, name: str) -> Any:
    return next(s for s in otel_spans.get_finished_spans() if s.name == name)


class TestUsageSpanWiring:
    """Without these listeners, Deepgram and Cartesia spend is invisible in Langfuse:
    LiveKit reports STT usage only to the OTel Metrics API (a no-op meter here) and
    TTS usage only in a custom attribute bag Langfuse's cost engine does not read.

    These drive `build_speech_components`, not `build_session`: the full session also
    builds google.LLM(vertexai=True), Silero VAD and the EnglishModel turn detector,
    which need ADC and downloaded model files. The billed pair is the part under test,
    and it constructs offline once the two plugin API keys are present."""

    @pytest.fixture(autouse=True)
    def _plugin_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both constructors raise ValueError without a key; neither opens a connection
        # until stream()/synthesize(), so dummy values keep construction offline.
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        monkeypatch.setenv("CARTESIA_API_KEY", "test-key")

    def test_cascade_tts_emits_a_usage_generation(self, otel_spans: Any) -> None:
        _stt, tts = build_speech_components()
        tts.emit("metrics_collected", _tts_metrics())
        span = _span(otel_spans, SPAN_TTS_USAGE)
        assert span.attributes["gen_ai.request.model"] == "sonic-3.5-2026-05-04"

    def test_cascade_stt_emits_a_usage_generation(self, otel_spans: Any) -> None:
        stt, _tts = build_speech_components()
        stt.emit("metrics_collected", _stt_metrics())
        span = _span(otel_spans, SPAN_STT_USAGE)
        assert json.loads(span.attributes["langfuse.observation.usage_details"]) == {
            "stt_audio_ms": 5000
        }

    def test_the_room_name_reaches_both_generations(self, otel_spans: Any) -> None:
        room = "call--00000000-0000-0000-0000-0000000000aa--00000000-0000-0000-0000-0000000000bb"
        stt, tts = build_speech_components(room_name=room)
        stt.emit("metrics_collected", _stt_metrics())
        tts.emit("metrics_collected", _tts_metrics())
        assert _span(otel_spans, SPAN_STT_USAGE).attributes["langfuse.session.id"] == room
        assert _span(otel_spans, SPAN_TTS_USAGE).attributes["langfuse.session.id"] == room
