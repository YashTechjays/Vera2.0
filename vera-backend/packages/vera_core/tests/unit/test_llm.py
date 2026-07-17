"""Unit tests for vera_core.llm — spec parsing, registry validation, fallback semantics.

Stub LLMs subclass the real livekit base classes so FallbackAdapter drives them
exactly as it would a production plugin.
"""

import pytest
from livekit.agents import APIConnectionError
from livekit.agents.llm import LLM, ChatChunk, ChoiceDelta, LLMStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from vera_core.llm import FallbackOptions, LLMSpec, LLMUnavailableError, ResilientLLM


class _StubStream(LLMStream):
    def __init__(self, llm, *, chat_ctx, conn_options, text, error):
        self._text = text
        self._error = error
        # super().__init__ starts the _run task immediately — set fields first.
        super().__init__(llm, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    async def _run(self) -> None:
        if self._error:
            raise APIConnectionError("stub failure")
        self._event_ch.send_nowait(
            ChatChunk(id="stub", delta=ChoiceDelta(role="assistant", content=self._text))
        )


class _StubLLM(LLM):
    def __init__(self, *, text: str = "", error: bool = False) -> None:
        super().__init__()
        self._text = text
        self._error = error
        self.calls = 0

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kwargs):
        self.calls += 1
        return _StubStream(
            self, chat_ctx=chat_ctx, conn_options=conn_options, text=self._text, error=self._error
        )


def _registry_for(*llms: _StubLLM):
    """A registry whose provider keys stub0, stub1, ... return the given LLMs."""
    return {f"stub{i}": (lambda spec, secrets, _llm=stub: _llm) for i, stub in enumerate(llms)}


def _specs(n: int) -> list[LLMSpec]:
    return [LLMSpec(provider=f"stub{i}", model="m") for i in range(n)]


def test_parse_selector() -> None:
    spec = LLMSpec.parse("google:gemini-3.1-flash-lite")
    assert spec == LLMSpec(provider="google", model="gemini-3.1-flash-lite")


@pytest.mark.parametrize("bad", ["", "google", ":model", "google:"])
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        LLMSpec.parse(bad)


def test_unknown_provider_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        ResilientLLM(LLMSpec(provider="nope", model="m"), registry={})


@pytest.mark.asyncio
async def test_primary_success_never_touches_fallback() -> None:
    primary = _StubLLM(text="primary answer")
    fallback = _StubLLM(text="fallback answer")
    specs = _specs(2)
    client = ResilientLLM(specs[0], [specs[1]], registry=_registry_for(primary, fallback))
    try:
        assert await client.complete(system="s", user="u") == "primary answer"
    finally:
        await client.aclose()
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_primary_failure_falls_back() -> None:
    primary = _StubLLM(error=True)
    fallback = _StubLLM(text="fallback answer")
    specs = _specs(2)
    client = ResilientLLM(
        specs[0],
        [specs[1]],
        options=FallbackOptions(attempt_timeout=1.0, max_retry_per_llm=0, retry_interval=0.0),
        registry=_registry_for(primary, fallback),
    )
    try:
        assert await client.complete(system="s", user="u") == "fallback answer"
    finally:
        await client.aclose()
    assert primary.calls >= 1
    assert fallback.calls == 1


def _raising_factory(spec, secrets):
    raise LookupError("secret not found: OPENAI_API_KEY")


@pytest.mark.asyncio
async def test_fallback_construction_failure_does_not_break_primary() -> None:
    """A fallback whose client can't be built (e.g. its API key secret is absent)
    is dropped from the chain — the healthy primary still serves."""
    primary = _StubLLM(text="primary answer")
    registry = {**_registry_for(primary), "broken": _raising_factory}
    client = ResilientLLM(
        _specs(1)[0],
        [LLMSpec(provider="broken", model="m")],
        registry=registry,
    )
    try:
        assert await client.complete(system="s", user="u") == "primary answer"
    finally:
        await client.aclose()
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_all_constructions_failing_raises_unavailable() -> None:
    client = ResilientLLM(
        LLMSpec(provider="broken", model="m"),
        registry={"broken": _raising_factory},
    )
    with pytest.raises(LLMUnavailableError):
        await client.complete(system="s", user="u")
    await client.aclose()


@pytest.mark.asyncio
async def test_all_providers_failing_raises_unavailable() -> None:
    specs = _specs(2)
    client = ResilientLLM(
        specs[0],
        [specs[1]],
        options=FallbackOptions(attempt_timeout=1.0, max_retry_per_llm=0, retry_interval=0.0),
        registry=_registry_for(_StubLLM(error=True), _StubLLM(error=True)),
    )
    try:
        with pytest.raises(LLMUnavailableError):
            await client.complete(system="s", user="u")
    finally:
        await client.aclose()
