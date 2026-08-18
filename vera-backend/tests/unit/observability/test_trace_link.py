"""Cross-process trace join (design §3.2). The guarantee under test is that a span
opened in the control plane lands in the SAME TRACE as the worker's call span —
Langfuse rolls cost up reliably per trace, and its session rollup renders $0.00 for
model-calculated cost (langfuse#15109), so this is what makes a per-call total real."""

import asyncio
from typing import Any

import pytest
from opentelemetry import trace

from tests.unit.conftest import FakeTraceLinkRedis
from vera_core.observability.otel_testing import assert_no_phi_values
from vera_core.observability.trace_link import (
    TRACE_LINK_TIMEOUT_SECONDS,
    TraceLinkStore,
    call_scoped_span,
    current_traceparent,
    remote_parent,
    trace_link_key,
)

_ROOM = "call--00000000-0000-0000-0000-0000000000aa--00000000-0000-0000-0000-0000000000bb"


_TRACEPARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


class _HangingRedis:
    """Reachable but wedged: every command hangs instead of raising."""

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        await asyncio.sleep(10)

    async def get(self, key: str) -> bytes | None:
        await asyncio.sleep(10)
        return None


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
        redis = FakeTraceLinkRedis()
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
        redis = FakeTraceLinkRedis()
        await TraceLinkStore(redis).publish(_ROOM, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert redis.ttls[trace_link_key(_ROOM)] >= 3600

    @pytest.mark.asyncio
    async def test_a_missing_key_resolves_to_none(self) -> None:
        assert await TraceLinkStore(FakeTraceLinkRedis()).resolve(_ROOM) is None

    @pytest.mark.asyncio
    async def test_a_redis_outage_never_raises(self) -> None:
        # Tracing must never break a call or an API request (design §6).
        store = TraceLinkStore(FakeTraceLinkRedis(fails=True))
        await store.publish(_ROOM, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert await store.resolve(_ROOM) is None

    @pytest.mark.asyncio
    async def test_non_utf8_bytes_resolve_to_none_rather_than_raise(self) -> None:
        # A read failure includes a decode failure, not just a Redis outage (design §6).
        assert await TraceLinkStore(_NonUtf8Redis()).resolve(_ROOM) is None


class TestAWedgedRedisCannotStallTheCaller:
    """`create_redis()` sets no socket timeout, so an outage that HANGS rather than
    raising is the dangerous shape: it would hold up a greeting, an HTTP request, or
    the post-call consumer. Both directions must be time-boxed."""

    @pytest.mark.asyncio
    async def test_a_hanging_publish_returns_and_does_not_raise(self) -> None:
        store = TraceLinkStore(_HangingRedis(), timeout_s=0.05)
        await asyncio.wait_for(store.publish(_ROOM, _TRACEPARENT), timeout=1.0)

    @pytest.mark.asyncio
    async def test_a_hanging_resolve_degrades_to_no_parent(self) -> None:
        store = TraceLinkStore(_HangingRedis(), timeout_s=0.05)
        assert await asyncio.wait_for(store.resolve(_ROOM), timeout=1.0) is None

    def test_the_default_budget_is_short(self) -> None:
        # A short fixed budget — not the general Redis timeout, which is unset — is
        # what keeps best-effort tracing off every caller's critical path.
        assert TRACE_LINK_TIMEOUT_SECONDS == 2.0


class TestCallScopedSpan:
    """The single seam every out-of-worker span for a call goes through. It decides
    two things that were previously re-decided at each call site: what the span is
    parented to, and that no exception text is ever recorded on it."""

    @pytest.mark.asyncio
    async def test_the_span_joins_the_calls_trace(self, otel_spans: Any) -> None:
        store = TraceLinkStore(FakeTraceLinkRedis())
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_trace_id = worker_span.get_span_context().trace_id
        assert traceparent is not None
        await store.publish(_ROOM, traceparent)

        async with call_scoped_span(tracer, "vera.thing", room_name=_ROOM, trace_links=store):
            pass

        span = next(s for s in otel_spans.get_finished_spans() if s.name == "vera.thing")
        assert span.context.trace_id == worker_trace_id

    @pytest.mark.asyncio
    async def test_an_absent_link_degrades_to_a_root_span(self, otel_spans: Any) -> None:
        # Degraded, not broken: the work still traces, it just forms its own trace.
        tracer = trace.get_tracer("test")
        store = TraceLinkStore(FakeTraceLinkRedis())
        async with call_scoped_span(tracer, "vera.thing", room_name=_ROOM, trace_links=store):
            pass
        assert (
            next(s for s in otel_spans.get_finished_spans() if s.name == "vera.thing").parent
            is None
        )

    @pytest.mark.asyncio
    async def test_no_store_at_all_degrades_to_a_root_span(self, otel_spans: Any) -> None:
        tracer = trace.get_tracer("test")
        async with call_scoped_span(tracer, "vera.thing", room_name=_ROOM, trace_links=None):
            pass
        assert (
            next(s for s in otel_spans.get_finished_spans() if s.name == "vera.thing").parent
            is None
        )

    @pytest.mark.asyncio
    async def test_the_span_carries_the_call_correlation(self, otel_spans: Any) -> None:
        tracer = trace.get_tracer("test")
        async with call_scoped_span(tracer, "vera.thing", room_name=_ROOM, trace_links=None):
            pass
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "vera.thing")
        assert span.attributes["vera.room"] == _ROOM

    @pytest.mark.asyncio
    async def test_an_exception_leaves_no_text_on_the_span(self, otel_spans: Any) -> None:
        # THE PHI guard. A provider error can embed the request payload — a transcript,
        # a supervisor's audio — and both record_exception and set_status_on_exception
        # would copy its message onto a span that leaves the trust boundary.
        tracer = trace.get_tracer("test")
        with pytest.raises(RuntimeError):
            async with call_scoped_span(tracer, "vera.thing", room_name=_ROOM, trace_links=None):
                raise RuntimeError("member id 123-45-6789 rejected by provider")

        span = next(s for s in otel_spans.get_finished_spans() if s.name == "vera.thing")
        # The shared denylist sweep: name, every attribute, the status description AND
        # every event's attributes — more than an inline check remembers to cover.
        assert_no_phi_values(span, "123-45-6789")
