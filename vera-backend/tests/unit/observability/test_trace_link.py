"""Cross-process trace join (design §3.2). The guarantee under test is that a span
opened in the control plane lands in the SAME TRACE as the worker's call span —
Langfuse rolls cost up reliably per trace, and its session rollup renders $0.00 for
model-calculated cost (langfuse#15109), so this is what makes a per-call total real."""

from typing import Any

import pytest
from opentelemetry import trace

from vera_core.observability.trace_link import (
    TraceLinkStore,
    current_traceparent,
    remote_parent,
    trace_link_key,
)

_ROOM = "call--00000000-0000-0000-0000-0000000000aa--00000000-0000-0000-0000-0000000000bb"


class _FakeRedis:
    """Minimal get/set stand-in; `fails` makes every call raise, standing in for a
    Redis outage."""

    def __init__(self, *, fails: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._fails = fails

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self._fails:
            raise ConnectionError("redis down")
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> bytes | None:
        if self._fails:
            raise ConnectionError("redis down")
        value = self.values.get(key)
        return value.encode() if value is not None else None


class _NonUtf8Redis:
    """Stands in for data corruption or a stray writer: `get` returns bytes that
    cannot be UTF-8 decoded."""

    async def get(self, key: str) -> bytes | None:
        return b"\xff\xfe"


class TestKey:
    def test_key_follows_the_per_call_convention(self) -> None:
        # Matches vera:call-plan:<room> / vera:summary:<room> / vera:call-events:<room>.
        assert trace_link_key(_ROOM) == f"vera:trace:{_ROOM}"


class TestCapture:
    def test_traceparent_is_captured_from_the_ambient_span(self, otel_spans: Any) -> None:
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as span:
            traceparent = current_traceparent()
            expected = span.get_span_context()
        assert traceparent is not None
        assert f"{expected.trace_id:032x}" in traceparent


class TestAdoption:
    def test_a_span_under_the_remote_parent_joins_the_original_trace(self, otel_spans: Any) -> None:
        """THE load-bearing assertion: same trace_id across a process boundary."""
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_ctx = worker_span.get_span_context()

        parent = remote_parent(traceparent)
        assert parent is not None
        with tracer.start_as_current_span("vera.post_call.eval", context=parent) as later:
            assert later.get_span_context().trace_id == worker_ctx.trace_id

    def test_absent_traceparent_degrades_to_a_root_span(self) -> None:
        # Graceful degradation: the eval still traces, just as its own trace.
        assert remote_parent(None) is None
        assert remote_parent("") is None

    def test_malformed_traceparent_degrades_to_a_root_span(self) -> None:
        assert remote_parent("not-a-traceparent") is None
        assert remote_parent("00-0000000000000000000000000000000-0000000000000000-01") is None


class TestStore:
    @pytest.mark.asyncio
    async def test_publish_then_resolve_round_trips(self, otel_spans: Any) -> None:
        redis = _FakeRedis()
        store = TraceLinkStore(redis)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_ctx = worker_span.get_span_context()
        assert traceparent is not None
        await store.publish(_ROOM, traceparent)

        parent = await store.resolve(_ROOM)
        assert parent is not None
        with tracer.start_as_current_span("later", context=parent) as later:
            assert later.get_span_context().trace_id == worker_ctx.trace_id

    @pytest.mark.asyncio
    async def test_publish_sets_a_ttl(self) -> None:
        # Sized for the longest post-call window: the sweeper can re-drive a stranded
        # job minutes after call.ended, long after the call itself is over.
        redis = _FakeRedis()
        await TraceLinkStore(redis).publish(_ROOM, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert redis.ttls[trace_link_key(_ROOM)] >= 3600

    @pytest.mark.asyncio
    async def test_a_missing_key_resolves_to_none(self) -> None:
        assert await TraceLinkStore(_FakeRedis()).resolve(_ROOM) is None

    @pytest.mark.asyncio
    async def test_a_redis_outage_never_raises(self) -> None:
        # Tracing must never break a call or an API request (design §6).
        store = TraceLinkStore(_FakeRedis(fails=True))
        await store.publish(_ROOM, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert await store.resolve(_ROOM) is None

    @pytest.mark.asyncio
    async def test_non_utf8_bytes_resolve_to_none_rather_than_raise(self) -> None:
        # A read failure includes a decode failure, not just a Redis outage (design §6).
        assert await TraceLinkStore(_NonUtf8Redis()).resolve(_ROOM) is None
