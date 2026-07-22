"""Unit tests for `vera_core.stt` — selector parsing and the construction-time
provider validation/tolerance in ResilientSTT (mirrors test coverage philosophy
of vera_core.llm.ResilientLLM, which this module's shape is modeled on).

The provider chain's actual FallbackAdapter integration (real STT clients) isn't
covered here — that needs either network access or a full fake of livekit's STT
protocol; it's covered by the manual whisper end-to-end test in the coaching plan.
This file covers what's cheaply and reliably unit-testable: selector parsing, the
unknown-provider guard, and the "every provider failed to even construct" path,
which never touches FallbackAdapter at all.
"""

from typing import TYPE_CHECKING, Any

import pytest

from vera_core.config.secrets import SecretProvider
from vera_core.stt import ResilientSTT, STTSpec, STTUnavailableError

if TYPE_CHECKING:
    import aiohttp
    from livekit.agents.stt import STT


def test_spec_parses_provider_colon_model() -> None:
    spec = STTSpec.parse("deepgram:flux-general-en")
    assert (spec.provider, spec.model) == ("deepgram", "flux-general-en")


@pytest.mark.parametrize("selector", ["deepgram", "deepgram:", ":flux", "", ":"])
def test_spec_parse_rejects_malformed_selectors(selector: str) -> None:
    with pytest.raises(ValueError, match="invalid STT selector"):
        STTSpec.parse(selector)


def test_unknown_provider_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown STT provider"):
        ResilientSTT(STTSpec(provider="nonexistent", model="x"), registry={})


def test_known_provider_accepted_at_construction_without_building_a_client() -> None:
    """Construction only validates provider names against the registry — it must
    not eagerly call any factory (clients need a running event loop, same rule
    as ResilientLLM)."""
    calls = 0

    def _factory(
        spec: STTSpec, secrets: SecretProvider | None, http_session: "aiohttp.ClientSession"
    ) -> "STT[Any]":
        nonlocal calls
        calls += 1
        raise AssertionError("should not be called at construction time")

    ResilientSTT(STTSpec(provider="fake", model="x"), registry={"fake": _factory})
    assert calls == 0


@pytest.mark.asyncio
async def test_all_providers_failing_at_construction_raises_unavailable() -> None:
    """A provider whose client can't be built (missing package/secret) must never
    take down the whole chain by itself — but an EMPTY chain (every provider
    failed) is the one fatal case."""

    def _boom(
        spec: STTSpec, secrets: SecretProvider | None, http_session: "aiohttp.ClientSession"
    ) -> "STT[Any]":
        raise RuntimeError("no secret configured")

    stt = ResilientSTT(
        STTSpec(provider="primary", model="x"),
        [STTSpec(provider="fallback", model="y")],
        registry={"primary": _boom, "fallback": _boom},
    )

    with pytest.raises(STTUnavailableError):
        stt._adapter()


@pytest.mark.asyncio
async def test_aclose_before_any_use_is_a_safe_no_op() -> None:
    stt = ResilientSTT(STTSpec(provider="deepgram", model="flux-general-en"))
    await stt.aclose()  # must not raise even though _adapter() was never called


@pytest.mark.asyncio
async def test_adapter_passes_a_real_open_session_to_each_provider() -> None:
    """Regression test: providers used outside the agent worker's job context
    (this class runs in the control plane) need an explicit http_session or
    they fail immediately trying to look up a job-scoped one that doesn't
    exist here."""
    import aiohttp

    seen: list[aiohttp.ClientSession] = []

    def _factory(
        spec: STTSpec, secrets: SecretProvider | None, http_session: "aiohttp.ClientSession"
    ) -> "STT[Any]":
        seen.append(http_session)
        raise RuntimeError("stub - only the session matters here")

    stt = ResilientSTT(STTSpec(provider="fake", model="x"), registry={"fake": _factory})
    with pytest.raises(STTUnavailableError):
        stt._adapter()

    assert len(seen) == 1
    assert isinstance(seen[0], aiohttp.ClientSession)
    assert not seen[0].closed


@pytest.mark.asyncio
async def test_aclose_closes_the_owned_http_session() -> None:
    import aiohttp

    seen: list[aiohttp.ClientSession] = []

    def _factory(
        spec: STTSpec, secrets: SecretProvider | None, http_session: "aiohttp.ClientSession"
    ) -> "STT[Any]":
        seen.append(http_session)
        raise RuntimeError("stub - only the session matters here")

    stt = ResilientSTT(STTSpec(provider="fake", model="x"), registry={"fake": _factory})
    with pytest.raises(STTUnavailableError):
        stt._adapter()

    await stt.aclose()
    assert seen[0].closed
