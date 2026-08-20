"""Seeder contract: idempotent upsert, never seed a zero, and never ship a Gemini
entry without a cached rate."""

import re
from typing import Any

import httpx
import pytest

from agent_worker.cascade import _CARTESIA_TTS_MODEL
from scripts.seed_langfuse_prices import (
    GEMINI_MODELS,
    MODELS,
    PINNED_SPEECH_MODELS,
    MissingRateError,
    build_payload,
    configured_models,
    matching_entry,
    project_mismatch,
    resolve_rates,
    seed,
    target_project,
    unpriced_models,
)
from vera_core.config.settings import Settings

_RATES = {
    "LANGFUSE_PRICE_STT_FLUX_PER_MS": "0.00000010833",
    "LANGFUSE_PRICE_STT_NOVA_PER_MS": "0.00000012833",
    "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER": "0.000022",
    # Per-model, not per-family: each Gemini model carries its own three rates.
    **{
        env_var: "0.0000003"
        for model in MODELS
        for env_var in model.env_vars.values()
        if "GEMINI" in env_var
    },
}

_BY_NAME = {m.model_name: m for m in MODELS}


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

    def test_a_zero_rate_refuses_to_seed(self) -> None:
        # $0.00 renders identically to a free tier, not to "unset" — reject it the
        # same as a missing rate rather than silently seeding it. Targets a cached
        # rate specifically: that is the one an operator is most tempted to zero out
        # when they have input/output rates but no contracted cache rate yet.
        cached_var = _BY_NAME["vera-gemini-3.6-flash"].env_vars["cached"]
        with pytest.raises(MissingRateError):
            resolve_rates({**_RATES, cached_var: "0"})

    def test_a_negative_rate_refuses_to_seed(self) -> None:
        with pytest.raises(MissingRateError):
            resolve_rates({**_RATES, "LANGFUSE_PRICE_STT_FLUX_PER_MS": "-0.001"})

    @pytest.mark.parametrize("raw", ["inf", "nan"])
    def test_a_non_finite_rate_refuses_to_seed(self, raw: str) -> None:
        # Both parse fine as float() and would otherwise serialize to invalid JSON.
        with pytest.raises(MissingRateError):
            resolve_rates({**_RATES, "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER": raw})


class TestPayload:
    def test_a_new_model_carries_no_model_id(self) -> None:
        payload = build_payload(MODELS[0], rates=resolve_rates(_RATES))
        assert "modelId" not in payload
        assert payload["modelName"] == MODELS[0].model_name

    def test_no_payload_carries_a_model_id(self) -> None:
        # There is no upsert: POST rejects a duplicate modelName even WITH the id
        # ("already exists in project"), and PUT/PATCH are 405. Sending one would
        # just be noise on a request that can only ever create.
        for model in MODELS:
            assert "modelId" not in build_payload(model, rates=resolve_rates(_RATES))

    def test_prices_use_the_usage_keys_the_instrumentation_sends(self) -> None:
        rates = resolve_rates(_RATES)
        flux = build_payload(_BY_NAME["vera-deepgram-flux"], rates=rates)
        assert set(flux["pricingTiers"][0]["prices"]) == {"stt_audio_ms"}
        gemini = build_payload(_BY_NAME["vera-gemini-3.6-flash"], rates=rates)
        assert set(gemini["pricingTiers"][0]["prices"]) == {"input", "output", "cached"}

    def test_patterns_match_the_models_vera_actually_uses(self) -> None:
        assert re.match(_BY_NAME["vera-deepgram-flux"].match_pattern, "flux-general-en")
        assert re.match(_BY_NAME["vera-deepgram-nova"].match_pattern, "nova-3")
        assert re.match(_BY_NAME["vera-cartesia-sonic"].match_pattern, _CARTESIA_TTS_MODEL)
        assert re.match(_BY_NAME["vera-gemini-2.5-flash"].match_pattern, "gemini-2.5-flash")
        assert re.match(
            _BY_NAME["vera-gemini-3.1-flash-lite"].match_pattern, "gemini-3.1-flash-lite"
        )

    def test_patterns_survive_a_model_version_bump(self) -> None:
        # Family patterns, not exact versions: an exact pattern would silently zero cost
        # on the next bump, and a missing match looks identical to "no data".
        assert re.match(_BY_NAME["vera-cartesia-sonic"].match_pattern, "sonic-4")


class _FakeResponse:
    def __init__(self, payload: dict[str, object], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient. `existing` is the full model listing the
    server would return, served 100-per-page like the real API."""

    def __init__(self, existing: list[dict[str, Any]], *, post_status: int = 200) -> None:
        self._existing = existing
        self._post_status = post_status
        self.posted: list[dict[str, object]] = []
        self.deleted: list[str] = []

    async def get(self, _url: str, params: dict[str, int]) -> _FakeResponse:
        limit, page = params["limit"], params.get("page", 1)
        start = (page - 1) * limit
        return _FakeResponse({"data": self._existing[start : start + limit]})

    async def post(self, _url: str, json: dict[str, object]) -> _FakeResponse:
        self.posted.append(json)
        return _FakeResponse({}, status_code=self._post_status)

    async def delete(self, url: str) -> _FakeResponse:
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _FakeResponse({})


def _filler(count: int) -> list[dict[str, Any]]:
    """Langfuse ships ~160 built-in model entries; they share the listing with ours."""
    return [{"modelName": f"built-in-{i}", "id": f"b{i}"} for i in range(count)]


def _live_listing() -> list[dict[str, Any]]:
    """The listing a project already seeded with exactly these rates would return."""
    rates = resolve_rates(_RATES)
    return [
        {
            "modelName": m.model_name,
            "id": f"id-{m.model_name}",
            **build_payload(m, rates=rates),
        }
        for m in MODELS
    ]


class TestIdempotentUpsert:
    """Re-running must converge, and this API gives no upsert to lean on: an existing
    entry has to be deleted before it can be recreated. The seam `build_payload`'s
    tests do NOT cover is whether `_existing_ids` FINDS what is already there."""

    @pytest.mark.asyncio
    async def test_a_first_run_posts_every_model_without_an_id(self) -> None:
        client = _FakeClient([])
        outcomes = await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert outcomes == {m.model_name: "created" for m in MODELS}
        assert client.deleted == []  # nothing to replace on a fresh project

    @pytest.mark.asyncio
    async def test_an_unchanged_entry_is_left_alone_rather_than_replaced(self) -> None:
        # Replacing means DELETE then POST, and between them the model has NO price at
        # all. A re-run that changes nothing must never open that window.
        client = _FakeClient(_live_listing())
        outcomes = await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert outcomes == {m.model_name: "unchanged" for m in MODELS}
        assert client.deleted == []
        assert client.posted == []

    @pytest.mark.asyncio
    async def test_a_changed_rate_replaces_the_entry(self) -> None:
        listing = _live_listing()
        tier = listing[0]["pricingTiers"][0]
        tier["prices"] = {key: value * 2 for key, value in tier["prices"].items()}
        client = _FakeClient(listing)
        await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert client.deleted == [f"id-{MODELS[0].model_name}"]
        assert len(client.posted) == 1

    @pytest.mark.asyncio
    async def test_an_unrecognised_shape_is_replaced_rather_than_skipped(self) -> None:
        listing = _live_listing()
        del listing[0]["pricingTiers"]
        client = _FakeClient(listing)
        await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert client.deleted == [f"id-{MODELS[0].model_name}"]

    @pytest.mark.asyncio
    async def test_a_failed_replace_reports_the_model_as_unpriced(self) -> None:
        # The destructive window actually opened: the entry was deleted and the
        # replacement POST failed, so this model now has no price at all.
        listing = _live_listing()
        listing[0]["pricingTiers"][0]["prices"] = {"bogus": 1.0}
        client = _FakeClient(listing, post_status=400)
        outcomes = await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert outcomes[MODELS[0].model_name] == "UNPRICED"

    @pytest.mark.asyncio
    async def test_one_bad_payload_does_not_abandon_the_remaining_models(self) -> None:
        # Aborting mid-tuple would turn one bad model into a project-wide blackout.
        listing = _live_listing()
        listing[0]["pricingTiers"][0]["prices"] = {"bogus": 1.0}
        client = _FakeClient(listing, post_status=400)
        outcomes = await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert len(client.posted) == 1  # only the one that needed replacing
        # every model still accounted for, none silently dropped by an early return
        assert set(outcomes) == {m.model_name for m in MODELS}

    @pytest.mark.asyncio
    async def test_existing_entries_are_found_beyond_the_first_page(self) -> None:
        # The regression this exists for: a real instance carries ~160 built-ins, so
        # our entries land on page 2. Reading only page 1 loses every entry, and the
        # "idempotent" re-run then fails on its first POST.
        client = _FakeClient(_filler(160) + _live_listing())
        outcomes = await seed(client, resolve_rates(_RATES))  # type: ignore[arg-type]
        assert outcomes == {m.model_name: "unchanged" for m in MODELS}
        assert client.posted == []  # all found, all unchanged


class TestPerModelCoverage:
    """Per-model entries buy accurate attribution but give up the family pattern's
    safety net: a model nobody listed matches nothing and renders blank cost, which
    reads as 'this surface is free'. `matching_entry` is what makes that detectable."""

    def test_every_gemini_model_vera_routes_to_has_its_own_entry(self) -> None:
        for model in GEMINI_MODELS:
            entry = matching_entry(model)
            assert entry is not None, model
            assert entry.model_name == f"vera-{model}"

    def test_each_gemini_model_is_priced_separately(self) -> None:
        # The point of the split: no two models may share a rate variable, or they
        # would silently be billed at one another's price.
        env_vars = [v for m in MODELS for v in m.env_vars.values() if "GEMINI" in v]
        assert len(env_vars) == len(set(env_vars)) == len(GEMINI_MODELS) * 3

    def test_an_unlisted_model_matches_nothing(self) -> None:
        # The failure the coverage warning exists to announce.
        assert matching_entry("gemini-4.0-ultra") is None

    def test_a_vertex_version_suffix_still_matches(self) -> None:
        # Langfuse's own built-in Gemini patterns allow @version; a pinned Vertex
        # model must not fall off its price entry.
        assert matching_entry("gemini-3.6-flash@20260101") is not None

    def test_speech_models_still_match_their_families(self) -> None:
        # Deepgram/Cartesia stay family-matched on purpose: their versions are
        # rate-compatible, so a bump must not zero the cost.
        for model, expected in (
            ("flux-general-en", "vera-deepgram-flux"),
            ("nova-3", "vera-deepgram-nova"),
            (_CARTESIA_TTS_MODEL, "vera-cartesia-sonic"),
        ):
            entry = matching_entry(model)
            assert entry is not None, model
            assert entry.model_name == expected

    def test_flux_and_nova_are_priced_independently(self) -> None:
        flux = _BY_NAME["vera-deepgram-flux"].env_vars["stt_audio_ms"]
        nova = _BY_NAME["vera-deepgram-nova"].env_vars["stt_audio_ms"]
        assert flux != nova


class TestPayloadShape:
    """The POST body Langfuse actually validates. Discovered by running the seeder
    against a live instance: the design assumed `pricingTiers[0].prices` sufficed,
    but the endpoint rejects that with a 400 naming four more required fields."""

    def test_a_unit_is_declared_and_matches_what_the_keys_measure(self) -> None:
        # Langfuse validates `unit` against its own enum. It must describe what the
        # usage keys actually count, or the UI reports the wrong dimension for money.
        by_unit = {m.model_name: m.unit for m in MODELS}
        assert by_unit["vera-deepgram-flux"] == "MILLISECONDS"
        assert by_unit["vera-deepgram-nova"] == "MILLISECONDS"
        assert by_unit["vera-cartesia-sonic"] == "CHARACTERS"
        assert by_unit["vera-gemini-3.6-flash"] == "TOKENS"

    def test_every_payload_carries_a_complete_default_tier(self) -> None:
        # The 400 that blocked the first real seed: name, priority, conditions and
        # isDefault are all required, and EXACTLY ONE tier must be the default.
        rates = resolve_rates(_RATES)
        for model in MODELS:
            payload = build_payload(model, rates=rates)
            assert payload["unit"] == model.unit
            tiers = payload["pricingTiers"]
            assert len(tiers) == 1, model.model_name
            tier = tiers[0]
            assert tier["isDefault"] is True
            assert isinstance(tier["name"], str) and tier["name"]
            assert isinstance(tier["priority"], int)
            assert tier["conditions"] == []
            assert tier["prices"]


class TestConfiguredModelCoverage:
    """`configured_models` is the safety net for the per-model pricing decision: a
    model Vera routes to that matches no entry renders blank cost, which reads as
    "this surface is free". Built from the REAL Settings, not a hand-copied stand-in —
    a stale copy stays green through exactly the drift the net exists to catch."""

    def test_the_provider_prefix_is_stripped(self) -> None:
        # Settings hold `google:gemini-3.1-flash-lite`, but LLMSpec.parse splits the
        # prefix off before the plugin sees it, so the SPAN carries the bare name.
        models = configured_models(Settings(_env_file=None))
        assert "gemini-3.1-flash-lite" in models
        assert not any(":" in m for m in models)

    def test_the_cascades_pinned_speech_models_are_covered(self) -> None:
        # They live in cascade.py, not Settings, so a Settings-only sweep would miss
        # the very STT/TTS surfaces this feature exists to price.
        models = configured_models(Settings(_env_file=None))
        assert set(PINNED_SPEECH_MODELS) <= set(models)

    def test_every_pinned_speech_model_has_a_price_entry(self) -> None:
        assert [m for m in PINNED_SPEECH_MODELS if matching_entry(m) is None] == []

    def test_the_shipped_defaults_leave_nothing_undecided(self) -> None:
        # Either priced, or named in KNOWN_UNPRICED with a reason. `just langfuse-verify`
        # exits non-zero otherwise, and a gate that is red on a healthy system is one
        # everybody learns to ignore.
        assert unpriced_models(Settings(_env_file=None)) == []

    def test_a_model_nobody_decided_about_is_still_reported(self) -> None:
        settings = Settings(_env_file=None)
        settings.observer_extract_primary_model = "google:gemini-9.9-flash"
        assert unpriced_models(settings) == ["gemini-9.9-flash"]


class _ProjectResponse:
    """The /api/public/projects reply. `is_error` is what the seeder branches on, so a
    401 from another environment's keys must behave like no answer, not like a project."""

    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400

    def json(self) -> dict[str, Any]:
        return self._payload


class _ProjectClient:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def get(self, _url: str) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestTargetProject:
    """The key pair is the only thing that selects a project, so a run has to be able to
    say which one it is about to change."""

    @pytest.mark.asyncio
    async def test_both_the_id_and_the_name_identify_the_project(self) -> None:
        client = _ProjectClient(
            _ProjectResponse({"data": [{"id": "proj-vera-test", "name": "Vera Test"}]})
        )
        assert await target_project(client) == (  # type: ignore[arg-type]
            "proj-vera-test",
            "Vera Test",
        )

    @pytest.mark.asyncio
    async def test_an_error_response_reports_no_project(self) -> None:
        client = _ProjectClient(_ProjectResponse({}, status_code=401))
        assert await target_project(client) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_unreachable_endpoint_reports_no_project(self) -> None:
        # A Langfuse without the endpoint must not stop a local seed from running.
        client = _ProjectClient(httpx.ConnectError("nope"))
        assert await target_project(client) is None  # type: ignore[arg-type]


class TestProjectGuard:
    """Naming a project is a request to change ONLY that one. Replacing an entry is
    DELETE-then-POST, so the wrong target does not merely add noise — it can leave that
    project's models with no price at all."""

    def test_a_matching_id_proceeds(self) -> None:
        assert project_mismatch("proj-vera-test", ("proj-vera-test", "Vera Test")) is None

    def test_a_matching_name_proceeds(self) -> None:
        assert project_mismatch("Vera Test", ("proj-vera-test", "Vera Test")) is None

    def test_the_wrong_project_is_refused_and_named(self) -> None:
        reason = project_mismatch("proj-vera-test", ("proj-vera-prod", "Vera Prod"))
        assert reason is not None
        assert "proj-vera-prod" in reason and "proj-vera-test" in reason

    @pytest.mark.parametrize("identifiers", [None, ()])
    def test_an_unconfirmable_project_fails_closed(self, identifiers: Any) -> None:
        assert project_mismatch("proj-vera-test", identifiers) is not None

    def test_naming_no_project_leaves_the_default_run_alone(self) -> None:
        assert project_mismatch(None, None) is None
        assert project_mismatch("", ("proj-vera-local",)) is None
