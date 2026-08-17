"""The post-call eval's Vertex calls emit zero spans today (design §2.5) — the largest
untraced LLM spend in the system. These assert the generation they now emit."""

import asyncio
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
    client._semaphore = asyncio.Semaphore(4)

    class _Models:
        async def generate_content(self, **_: Any) -> _FakeResponse:
            return response

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    client._client = _Client()  # type: ignore[assignment]
    return client


def _span(exporter: Any) -> Any:
    return next(s for s in exporter.get_finished_spans() if s.name == SPAN_EVAL_GENERATE)


def _usage(exporter: Any) -> dict[str, int]:
    # The literal attribute name, not the constant: this IS the Langfuse wire contract,
    # so a rename of the constant must still fail here.
    usage: dict[str, int] = json.loads(
        _span(exporter).attributes["langfuse.observation.usage_details"]
    )
    return usage


@pytest.mark.asyncio
class TestEvalGeneration:
    async def test_a_generation_is_emitted_with_token_usage(self, otel_spans: Any) -> None:
        client = _client(_FakeResponse("[]", _FakeUsage(8412, 611, 0, 0)))
        await client._generate("prompt", {}, pass_name="extract")
        assert _usage(otel_spans) == {"input": 8412, "output": 611}

    async def test_cached_tokens_are_split_out_of_input(self, otel_spans: Any) -> None:
        # prompt_token_count INCLUDES cached tokens; sending it whole alongside `cached`
        # would double-count them (design §5.4).
        client = _client(_FakeResponse("[]", _FakeUsage(10000, 500, 9000, 0)))
        await client._generate("prompt", {}, pass_name="extract")
        usage = _usage(otel_spans)
        assert usage == {"input": 1000, "cached": 9000, "output": 500}
        assert usage["input"] + usage["cached"] == 10000

    async def test_thinking_tokens_bill_as_output(self, otel_spans: Any) -> None:
        # gemini-2.5-flash is a thinking model and Vera configures thinking on it.
        client = _client(_FakeResponse("[]", _FakeUsage(100, 20, 0, 80)))
        await client._generate("prompt", {}, pass_name="judge")
        assert _usage(otel_spans) == {"input": 100, "output": 100}

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
