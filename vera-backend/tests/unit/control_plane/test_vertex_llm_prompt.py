import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from control_plane import llm as llm_mod
from control_plane.llm import (
    _JUDGE_MAX_ATTEMPTS,
    VertexLLMClient,
    _turns_block,
    build_extract_prompt,
    build_judge_prompt,
    parse_extract_response,
    parse_judge_response,
)
from vera_core.integrations.llm import ExtractedField, TranscriptTurn

_loads_response = VertexLLMClient._loads_response


def _ef(path: str) -> ExtractedField:
    return ExtractedField(field_path=path, value="yes", confidence=80, evidence_seq=0)


def _vd(path: str) -> dict[str, Any]:
    return {"field_path": path, "supported": True, "confidence": 90, "evidence": "ok"}


# Each queued item is either a response list OR an Exception to raise on that _generate call.
_Queued = list[dict[str, Any]] | Exception


class _StubJudgeClient(VertexLLMClient):
    """VertexLLMClient with the Gemini call stubbed — each _generate returns (or
    raises) the next queued item and records the (prompt, schema) it saw."""

    def __init__(self, responses: list[_Queued]) -> None:
        self._responses = list(responses)
        self._model = "fake-model"
        self.generate_calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self, prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        self.generate_calls.append((prompt, schema))
        item: _Queued = self._responses.pop(0) if self._responses else []
        if isinstance(item, Exception):
            raise item
        return item


def _enum_of(call: tuple[str, dict[str, Any]]) -> list[str]:
    return cast(list[str], call[1]["items"]["properties"]["field_path"]["enum"])


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the error-retry backoff so retry tests don't sleep for real."""
    monkeypatch.setattr(llm_mod, "_JUDGE_RETRY_BACKOFF_S", 0.0)


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record asyncio.sleep delays, with the backoff set to a distinctive 7.0."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(llm_mod, "_JUDGE_RETRY_BACKOFF_S", 7.0)
    monkeypatch.setattr("asyncio.sleep", _recording_sleep)
    return sleeps


def _fake_genai_client(kwargs_sink: dict[str, Any], generate_content: Any = None) -> type:
    """A genai.Client stand-in that records ctor kwargs and serves generate_content."""

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            kwargs_sink.update(kwargs)
            self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))

    return _FakeClient


def test_extract_prompt_numbers_turns_and_lists_paths() -> None:
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    prompt = build_extract_prompt(["sections.cov.network_status"], turns)
    assert "sections.cov.network_status" in prompt
    assert "[0]" in prompt and "[1]" in prompt  # evidence_seq anchors


def test_extract_prompt_states_the_percent_shape_and_says_nothing_about_money() -> None:
    """This prompt carries no field metadata — only bare paths — so the unit convention
    has to be spelled out, matching the Observer's rule in agent_worker/observer.py.

    Money is deliberately absent: `currency` leaves have no normalizer and no backfill, so
    instructing the model to switch to "$20" would change money's stored shape with nothing
    to converge it — worse than leaving currency alone (PR #82 review)."""
    prompt = build_extract_prompt(["sections.cov.coinsurance"], [])
    assert '"20%", never "20"' in prompt
    assert "$20" not in prompt


def test_parse_extract_response_maps_fields() -> None:
    data = [{"field_path": "p", "value": "in-network", "confidence": 90, "evidence_seq": 1}]
    out = parse_extract_response(data)
    assert out[0].field_path == "p" and out[0].evidence_seq == 1


def test_parse_extract_response_drops_blank_values() -> None:  # VR2-93
    data = [
        {"field_path": "a", "value": "", "confidence": 90, "evidence_seq": 1},
        {"field_path": "b", "value": "   ", "confidence": 90, "evidence_seq": 2},
        {"field_path": "c", "value": "No", "confidence": 90, "evidence_seq": 3},
    ]
    out = parse_extract_response(data)
    assert [e.field_path for e in out] == ["c"]


def test_build_judge_prompt_includes_extracted_and_transcript() -> None:
    extracted = [ExtractedField(field_path="a.b", value="yes", confidence=80, evidence_seq=0)]
    turns = [TranscriptTurn(0, "agent", "yes, covered")]
    prompt = build_judge_prompt(extracted, _turns_block(turns))
    assert "a.b" in prompt
    assert "[0]" in prompt


def test_parse_judge_response_maps_verdicts() -> None:
    data = [{"field_path": "a.b", "supported": True, "confidence": 95, "evidence": "yes, covered"}]
    out = parse_judge_response(data)
    assert out[0].field_path == "a.b"
    assert out[0].supported is True
    assert out[0].evidence == "yes, covered"


def test_build_judge_prompt_excludes_extractor_confidence() -> None:
    """Judge prompt must NOT leak extraction confidence—only value, field_path, evidence_seq.
    A distinctive confidence value (77) should NOT appear in the prompt."""
    extracted = [
        ExtractedField(
            field_path="sections.cov.network_status",
            value="in-network",
            confidence=77,  # Distinctive; must NOT appear in the prompt
            evidence_seq=1,
        )
    ]
    turns = [TranscriptTurn(0, "agent", "you are in network")]
    prompt = build_judge_prompt(extracted, _turns_block(turns))

    # The prompt MUST include the value and field_path and evidence_seq
    assert "in-network" in prompt
    assert "sections.cov.network_status" in prompt
    assert "evidence_seq" in prompt  # evidence_seq key must be present
    assert '"1"' in prompt or ": 1" in prompt  # evidence_seq value (1) must be present

    # The prompt MUST NOT include the extraction confidence (77)
    assert "77" not in prompt
    # confidence field must not appear in the extracted items JSON
    assert '"confidence": 77' not in prompt


async def test_judge_single_pass_when_all_fields_covered() -> None:
    """No re-judge when the first pass returns a verdict for every field."""
    stub = _StubJudgeClient([[_vd("a"), _vd("b")]])
    out = await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b"]
    assert len(stub.generate_calls) == 1


async def test_judge_rejudges_only_the_dropped_fields() -> None:
    """When Gemini drops part of a batch, judge re-runs on just the missing
    subset until every asked field has a verdict."""
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    stub = _StubJudgeClient([[_vd("a")], [_vd("b"), _vd("c")]])
    out = await stub.judge(extracted=extracted, turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]
    assert len(stub.generate_calls) == 2


async def test_judge_enum_constrains_field_path_to_pending_paths() -> None:
    """Each pass constrains field_path to exactly the still-unjudged paths, so a
    verdict can never carry a reworded path — and the retry batch shrinks."""
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    stub = _StubJudgeClient([[_vd("a")], [_vd("b"), _vd("c")]])
    await stub.judge(extracted=extracted, turns=[])
    assert _enum_of(stub.generate_calls[0]) == ["a", "b", "c"]
    assert _enum_of(stub.generate_calls[1]) == ["b", "c"]
    # Vertex only enforces the enum when format is "enum" (google-genai contract).
    field_path_schema = stub.generate_calls[0][1]["items"]["properties"]["field_path"]
    assert field_path_schema["format"] == "enum"


async def test_judge_stops_when_an_attempt_makes_no_progress() -> None:
    """Once a re-judge of the still-missing paths yields no new verdict, bail —
    retrying the same paths again won't help."""
    stub = _StubJudgeClient([[_vd("a")], []])  # 'b' never comes back
    out = await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])
    assert [v.field_path for v in out] == ["a"]
    assert len(stub.generate_calls) == 2


async def test_judge_is_bounded_by_max_attempts() -> None:
    """Even when every attempt makes SOME progress, the loop never exceeds the cap;
    a field still missing at the cap is returned as partial coverage."""
    extracted = [_ef("a"), _ef("b"), _ef("c"), _ef("d")]
    stub = _StubJudgeClient([[_vd("a")], [_vd("b")], [_vd("c")], [_vd("d")]])
    out = await stub.judge(extracted=extracted, turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]  # 'd' unreached at the cap
    assert len(stub.generate_calls) == _JUDGE_MAX_ATTEMPTS


async def test_judge_returns_empty_without_calling_llm_when_nothing_extracted() -> None:
    stub = _StubJudgeClient([])
    out = await stub.judge(extracted=[], turns=[])
    assert out == []
    assert stub.generate_calls == []


async def test_judge_chunks_large_batches_to_bound_enum_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch larger than the chunk size is split across calls, each enum-capped —
    a 180-value enum can 400 on Vertex and sink the whole form to review."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 2)
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    stub = _StubJudgeClient([[_vd("a"), _vd("b")], [_vd("c")]])
    out = await stub.judge(extracted=extracted, turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]
    assert len(stub.generate_calls) == 2  # two chunks, one attempt
    assert all(len(_enum_of(c)) <= 2 for c in stub.generate_calls)


async def test_judge_raises_when_a_chunk_error_leaves_coverage_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that keeps failing so some fields never get a verdict must raise
    (→ LLM_ERROR review), never return partial — a silently-unjudged required field
    reads as unsatisfied and redials the payer for data already collected."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 2)
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    # chunk 1 (a,b) succeeds; chunk 2 (c) fails every attempt → 'c' never covered.
    responses: list[_Queued] = [[_vd("a"), _vd("b")], *([RuntimeError("boom")] * 4)]
    stub = _StubJudgeClient(responses)
    with pytest.raises(RuntimeError, match="boom"):
        await stub.judge(extracted=extracted, turns=[])


async def test_judge_recovers_when_a_transient_chunk_error_clears_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that errors once but succeeds on a later attempt yields full coverage
    and must NOT raise — the error is incidental once every field is judged."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 2)
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    # attempt 1: chunk (a,b) ok, chunk (c) errors; attempt 2: chunk (c) succeeds.
    responses: list[_Queued] = [[_vd("a"), _vd("b")], RuntimeError("blip"), [_vd("c")]]
    stub = _StubJudgeClient(responses)
    out = await stub.judge(extracted=extracted, turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]


async def test_judge_raises_when_every_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A total wipe caused by errors must surface as an exception so the caller
    routes to LLM_ERROR review — never silently return [] and let the payer be redialed."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 2)
    stub = _StubJudgeClient([RuntimeError("boom")] * 6)
    with pytest.raises(RuntimeError, match="boom"):
        await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])


async def test_judge_ignores_verdicts_outside_the_asked_chunk() -> None:
    """A verdict for a path not in the chunk (an unenforced-enum rewording) is
    dropped, never overwriting or masquerading as coverage."""
    extracted = [_ef("a"), _ef("b")]
    stub = _StubJudgeClient([[_vd("a"), _vd("zzz.bogus"), _vd("b")]])
    out = await stub.judge(extracted=extracted, turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b"]


def test_loads_response_parses_valid_json() -> None:
    """_loads_response returns parsed list for valid JSON text."""
    data = '[{"field_path": "a.b", "value": "yes", "confidence": 90, "evidence_seq": 0}]'
    result = _loads_response(data)
    assert result[0]["field_path"] == "a.b"


def test_loads_response_raises_on_none() -> None:
    """_loads_response raises RuntimeError on None (safety-blocked response)."""
    with pytest.raises(RuntimeError, match="empty LLM response"):
        _loads_response(None)


def test_loads_response_raises_on_empty_string() -> None:
    """_loads_response raises RuntimeError on empty string."""
    with pytest.raises(RuntimeError, match="empty LLM response"):
        _loads_response("")


class _ConcurrencyProbeClient(VertexLLMClient):
    """Stub whose _generate blocks until EVERY expected chunk call has started —
    a sequential judge deadlocks here and trips the wait_for timeout."""

    def __init__(self, expected_calls: int) -> None:
        self._model = "fake-model"
        self._expected = expected_calls
        self._started = 0
        self._all_started = asyncio.Event()

    async def _generate(self, prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        self._started += 1
        if self._started == self._expected:
            self._all_started.set()
        await asyncio.wait_for(self._all_started.wait(), timeout=0.5)
        paths = cast(list[str], schema["items"]["properties"]["field_path"]["enum"])
        return [_vd(p) for p in paths]


async def test_judge_runs_chunks_of_one_attempt_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequential chunk execution — the latency bug this guards against — deadlocks
    against the probe's barrier and fails; concurrent chunks all start and pass."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 1)
    # Single attempt — a retry pass would rescue timed-out chunks and mask a sequential regression.
    monkeypatch.setattr(llm_mod, "_JUDGE_MAX_ATTEMPTS", 1)
    stub = _ConcurrencyProbeClient(expected_calls=3)
    out = await stub.judge(extracted=[_ef("a"), _ef("b"), _ef("c")], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]


async def test_judge_bounds_concurrent_vertex_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_concurrency caps in-flight generate_content calls — without the cap, a
    burst of chunks (x the consumer's 16-job gather) trips the Vertex quota."""
    in_flight = 0
    peak = 0

    async def _fake_generate_content(*, model: str, contents: str, config: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        paths = cast(list[str], config.response_schema["items"]["properties"]["field_path"]["enum"])
        return SimpleNamespace(text=json.dumps([_vd(p) for p in paths]))

    monkeypatch.setattr(
        "control_plane.llm.genai.Client", _fake_genai_client({}, _fake_generate_content)
    )
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 1)
    client = VertexLLMClient(project="p", location="l", model="m", max_concurrency=2)
    out = await client.judge(extracted=[_ef(p) for p in "abcd"], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c", "d"]
    assert peak == 2


async def test_judge_retries_a_fully_errored_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """One transient blip failing EVERY chunk of an attempt (concurrent chunks fail
    together) must not forfeit the remaining attempts."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 1)
    responses: list[_Queued] = [
        RuntimeError("burst"),
        RuntimeError("burst"),
        [_vd("a")],
        [_vd("b")],
    ]
    stub = _StubJudgeClient(responses)
    out = await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b"]


async def test_judge_backs_off_before_retrying_an_errored_attempt(
    monkeypatch: pytest.MonkeyPatch, recorded_sleeps: list[float]
) -> None:
    """An errored attempt retries only after the backoff pause — an instant re-fire
    lands in the same exhausted quota window."""
    monkeypatch.setattr(llm_mod, "_JUDGE_CHUNK_SIZE", 2)
    stub = _StubJudgeClient([[_vd("a"), _vd("b")], RuntimeError("blip"), [_vd("c")]])
    out = await stub.judge(extracted=[_ef("a"), _ef("b"), _ef("c")], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]
    assert recorded_sleeps == [7.0]


async def test_judge_retries_dropped_fields_without_backoff(
    recorded_sleeps: list[float],
) -> None:
    """A drop-item retry (no error, model just omitted fields) is not rate-limit
    shaped and must stay immediate."""
    stub = _StubJudgeClient([[_vd("a")], [_vd("b")]])
    out = await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b"]
    assert recorded_sleeps == []


def test_vertex_client_sets_http_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The genai client must carry a request timeout — without one, a stalled
    Vertex call holds the form in AI_PROCESSING until the sweeper's 5-min grace."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr("control_plane.llm.genai.Client", _fake_genai_client(captured))
    VertexLLMClient(project="p", location="l", model="m", timeout_ms=45_000)
    assert captured["http_options"].timeout == 45_000

    captured.clear()
    VertexLLMClient(project="p", location="l", model="m")
    assert captured["http_options"].timeout == 120_000
