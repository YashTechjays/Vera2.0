"""Seeder contract: idempotent upsert, never seed a zero, and never ship a Gemini
entry without a cached rate."""

import re

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
        assert re.match(by_name["vera-cartesia-sonic"].match_pattern, "sonic-3.5-2026-05-04")
        assert re.match(by_name["vera-gemini"].match_pattern, "gemini-2.5-flash")
        assert re.match(by_name["vera-gemini"].match_pattern, "gemini-3.1-flash-lite")

    def test_patterns_survive_a_model_version_bump(self) -> None:
        # Family patterns, not exact versions: an exact pattern would silently zero cost
        # on the next bump, and a missing match looks identical to "no data".
        by_name = {m.model_name: m for m in MODELS}
        assert re.match(by_name["vera-cartesia-sonic"].match_pattern, "sonic-4")
