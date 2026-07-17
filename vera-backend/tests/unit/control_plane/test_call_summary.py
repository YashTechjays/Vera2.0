"""Unit tests for call_summary: diarized formatting, Redis-first snapshot,
cache orchestration. The DB-fallback branch of snapshot_turns needs live
Postgres and is covered by the endpoint integration tests."""

from collections.abc import AsyncIterator

import pytest

from control_plane.call_summary import (
    CallSummaryResponse,
    SummaryCache,
    SummaryLLM,
    SummarySections,
    TranscriptTurn,
    format_diarized,
    snapshot_turns,
    summarize_call,
)
from vera_core.call_stream import (
    TYPE_CALL_STATUS,
    TYPE_TRANSCRIPT,
    CallStreamEvent,
    CallStreamService,
)
from vera_core.db import uuid7
from vera_core.observability.correlation import room_name_for_call


class _FakeStreamStore:
    """read_all-only CallStreamStore; other protocol methods unused here."""

    def __init__(self, events: list[CallStreamEvent]) -> None:
        self._events = events

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return self._events

    async def publish(self, room_name: str, event: CallStreamEvent) -> None: ...
    async def mark_ended(self, room_name: str) -> None: ...
    async def delete(self, room_name: str) -> None: ...
    async def exists(self, room_name: str) -> bool:
        return bool(self._events)

    async def read(
        self, room_name: str, *, first_entry_deadline_s: float | None = None
    ) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        return
        yield  # pragma: no cover — never invoked; read_all is this fake's only reader


class _DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.set_ttls: list[int] = []

    async def get(self, room_name: str) -> str | None:
        return self.data.get(room_name)

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        self.data[room_name] = payload
        self.set_ttls.append(ttl_seconds)


class _BrokenCache:
    async def get(self, room_name: str) -> str | None:
        raise ConnectionError("redis down")

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        raise ConnectionError("redis down")


class _StubLLM:
    def __init__(self, text: str = "the summary") -> None:
        self.text = text
        self.calls = 0

    async def complete(self, *, system: str, user: str) -> str:
        self.calls += 1
        self.last_user = user
        return self.text


def _turn_event(source: str, role: str, text: str, ts: int = 1) -> CallStreamEvent:
    return CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": role, "source": source, "text": text}, ts=ts
    )


def test_format_diarized_labels_speakers() -> None:
    turns = [
        TranscriptTurn(source="bot", role="agent", text="Hello, calling about a claim."),
        TranscriptTurn(source="rep", role="user", text="Member ID please?"),
        TranscriptTurn(source="bot", role="dtmf", text="1234"),
        TranscriptTurn(source="supervisor", role="agent", text="Taking over."),
    ]
    assert format_diarized(turns) == (
        "Vera (agent): Hello, calling about a claim.\n"
        "Payer rep: Member ID please?\n"
        "Vera (agent) [keypad]: 1234\n"
        "Supervisor: Taking over."
    )


@pytest.mark.asyncio
async def test_snapshot_prefers_live_stream_and_filters_non_transcript() -> None:
    events = [
        _turn_event("bot", "agent", "hi"),
        CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "active"}, ts=2),
        _turn_event("rep", "user", "hello"),
    ]
    stream = CallStreamService(_FakeStreamStore(events))
    turns = await snapshot_turns(stream, None, uuid7(), uuid7())  # sessionmaker unused
    assert turns == [
        TranscriptTurn(source="bot", role="agent", text="hi"),
        TranscriptTurn(source="rep", role="user", text="hello"),
    ]


async def _summarize(
    stream_events: list[CallStreamEvent], cache: SummaryCache, llm: SummaryLLM, ttl: int = 5
) -> CallSummaryResponse:
    tenant_id, call_id = uuid7(), uuid7()
    return await summarize_call(
        llm=llm,
        cache=cache,
        stream=CallStreamService(_FakeStreamStore(stream_events)),
        sessionmaker=None,  # Redis path only in unit tests
        tenant_id=tenant_id,
        call_id=call_id,
        ttl_seconds=ttl,
    )


@pytest.mark.asyncio
async def test_summarize_ready_and_cached() -> None:
    cache, llm = _DictCache(), _StubLLM()
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await _summarize(events, cache, llm)
    assert result.status == "ready"
    assert result.summary == "the summary"
    assert result.turn_count == 2
    assert llm.calls == 1
    assert cache.set_ttls == [5]
    assert "Payer rep: hello" in llm.last_user


@pytest.mark.asyncio
async def test_summarize_cache_hit_skips_llm() -> None:
    cache, llm = _DictCache(), _StubLLM()
    tenant_id, call_id = uuid7(), uuid7()
    cached = CallSummaryResponse(status="ready", summary="old", generated_at=1, turn_count=2)
    cache.data[room_name_for_call(tenant_id, call_id)] = cached.model_dump_json()
    result = await summarize_call(
        llm=llm,
        cache=cache,
        stream=CallStreamService(_FakeStreamStore([])),
        sessionmaker=None,
        tenant_id=tenant_id,
        call_id=call_id,
        ttl_seconds=5,
    )
    assert result == cached
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_summarize_pending_below_min_turns_no_llm_no_cache() -> None:
    cache, llm = _DictCache(), _StubLLM()
    result = await _summarize([_turn_event("bot", "agent", "hi")], cache, llm)
    assert result.status == "pending"
    assert result.summary is None
    assert llm.calls == 0
    assert cache.data == {}


@pytest.mark.asyncio
async def test_summarize_dtmf_not_counted_as_speech() -> None:
    cache, llm = _DictCache(), _StubLLM()
    events = [_turn_event("bot", "agent", "hi"), _turn_event("bot", "dtmf", "1")]
    result = await _summarize(events, cache, llm)
    assert result.status == "pending"
    assert result.turn_count == 2


@pytest.mark.asyncio
async def test_cache_failure_degrades_to_fresh_compute() -> None:
    llm = _StubLLM()
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await _summarize(events, _BrokenCache(), llm)
    assert result.status == "ready"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_cache_invalid_payload_degrades_to_fresh_compute() -> None:
    """A corrupt/schema-skewed cached payload must not raise pydantic ValidationError
    up to the global handler (whose repr would leak the cached summary text as PHI) —
    it's treated as a cache miss instead."""
    cache, llm = _DictCache(), _StubLLM()
    tenant_id, call_id = uuid7(), uuid7()
    cache.data[room_name_for_call(tenant_id, call_id)] = "not json"
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await summarize_call(
        llm=llm,
        cache=cache,
        stream=CallStreamService(_FakeStreamStore(events)),
        sessionmaker=None,
        tenant_id=tenant_id,
        call_id=call_id,
        ttl_seconds=5,
    )
    assert result.status == "ready"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_summarize_parses_json_sections_and_flattens_summary() -> None:
    cache, llm = _DictCache(), _StubLLM(
        text=(
            '{"participants": "Vera and payer IVR", "purpose": "verify benefits",'
            ' "facts": ["member ID confirmed"], "open_items": ["awaiting DOB"],'
            ' "next_step": "provide DOB"}'
        )
    )
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await _summarize(events, cache, llm)
    assert result.status == "ready"
    assert result.sections == SummarySections(
        participants="Vera and payer IVR",
        purpose="verify benefits",
        facts=["member ID confirmed"],
        open_items=["awaiting DOB"],
        next_step="provide DOB",
    )
    assert result.summary is not None
    assert "Established:" in result.summary
    assert "- member ID confirmed" in result.summary


@pytest.mark.asyncio
async def test_summarize_non_json_reply_falls_back_to_raw_text() -> None:
    cache, llm = _DictCache(), _StubLLM(text="just prose, not JSON")
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await _summarize(events, cache, llm)
    assert result.status == "ready"
    assert result.sections is None
    assert result.summary == "just prose, not JSON"
