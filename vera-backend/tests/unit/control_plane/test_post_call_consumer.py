from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace

from control_plane import post_call_consumer
from control_plane.call_summary import TranscriptTurn as StreamTurn
from control_plane.post_call_consumer import PostCallConsumer, build_turns
from tests.conftest import FakeTraceLinkRedis
from vera_core.events import PostCallJob
from vera_core.integrations.llm import TranscriptTurn
from vera_core.models.enums import FormStatus
from vera_core.observability import TraceLinkStore, room_name_for_call
from vera_core.observability.trace_link import current_traceparent
from vera_core.services.post_call_eval import EvalOutcome


@pytest.mark.asyncio
async def test_build_turns_enumerates_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_turns adapts dev's (source, role, text) snapshot turns into the eval's
    seq-indexed TranscriptTurn, in order — seq is the evidence pointer."""

    async def fake_snapshot_turns(*_args: Any, **_kwargs: Any) -> list[StreamTurn]:
        return [
            StreamTurn(source="rep", role="user", text="hello"),
            StreamTurn(source="bot", role="agent", text="in network"),
        ]

    monkeypatch.setattr(post_call_consumer, "snapshot_turns", fake_snapshot_turns)

    turns = await build_turns(None, None, uuid4(), uuid4())  # type: ignore[arg-type]

    assert turns == [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]


@pytest.mark.asyncio
async def test_dispatch_pass_runs_after_the_eval_session_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_call no longer dispatches — the consumer does, once its transaction
    has committed. Dispatching from inside it would place calls whose Call rows the
    worker-event consumer cannot see yet."""
    order: list[str] = []

    class _Session:
        async def __aenter__(self) -> "_Session":
            order.append("session-open")
            return self

        async def __aexit__(self, *exc: object) -> None:
            order.append("session-commit")

    async def fake_build_turns(*_args: Any, **_kwargs: Any) -> list[TranscriptTurn]:
        return []

    async def fake_evaluate_call(*_args: Any, **_kwargs: Any) -> EvalOutcome:
        order.append("evaluate")
        return EvalOutcome(status=FormStatus.EXCEPTION_REVIEW, answers_written=0)

    async def fake_run_dispatch_pass(*_args: Any, **_kwargs: Any) -> None:
        order.append("dispatch")

    monkeypatch.setattr(post_call_consumer, "build_turns", fake_build_turns)
    monkeypatch.setattr(post_call_consumer, "tenant_session", lambda sm, tid: _Session())
    monkeypatch.setattr(post_call_consumer, "evaluate_call", fake_evaluate_call)
    monkeypatch.setattr(post_call_consumer, "run_dispatch_pass", fake_run_dispatch_pass)

    consumer = PostCallConsumer(
        redis=None,  # type: ignore[arg-type]
        sessionmaker=None,  # type: ignore[arg-type]
        call_stream=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        livekit=object(),
    )
    job = PostCallJob(tenant_id=uuid4(), form_id=uuid4(), call_id=uuid4())

    await consumer._process_job(job)

    assert order == ["session-open", "evaluate", "session-commit", "dispatch"]


@pytest.mark.asyncio
async def test_a_stale_job_does_not_run_a_dispatch_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-job branches return without touching the form, so no slot was freed.
    A pass there would take the per-tenant advisory lock and serialize a real one behind
    a job that decided nothing."""
    dispatched: list[object] = []

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def fake_build_turns(*_args: Any, **_kwargs: Any) -> list[TranscriptTurn]:
        return []

    async def fake_evaluate_call(*_args: Any, **_kwargs: Any) -> EvalOutcome:
        return EvalOutcome(status=FormStatus.AI_PROCESSING, answers_written=0, transitioned=False)

    async def fake_run_dispatch_pass(*_args: Any, **_kwargs: Any) -> None:
        dispatched.append(object())

    monkeypatch.setattr(post_call_consumer, "build_turns", fake_build_turns)
    monkeypatch.setattr(post_call_consumer, "tenant_session", lambda sm, tid: _Session())
    monkeypatch.setattr(post_call_consumer, "evaluate_call", fake_evaluate_call)
    monkeypatch.setattr(post_call_consumer, "run_dispatch_pass", fake_run_dispatch_pass)

    consumer = PostCallConsumer(
        redis=None,  # type: ignore[arg-type]
        sessionmaker=None,  # type: ignore[arg-type]
        call_stream=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        livekit=object(),
    )

    await consumer._process_job(PostCallJob(tenant_id=uuid4(), form_id=uuid4(), call_id=uuid4()))

    assert dispatched == []


class TestPostCallEvalTraceJoin:
    """The post-call eval is the second-largest LLM layer of a call and runs minutes
    after it ends, in another process. Unparented, its whole Vertex bill lands in an
    orphan trace that no per-call cost query can find.

    Only the wiring is asserted here — that this consumer hands `call_scoped_span` the
    right room and store. The degradation contract belongs to the seam and is tested
    once, in `test_trace_link.py`."""

    @pytest.mark.asyncio
    async def test_the_eval_span_joins_the_calls_trace(
        self, otel_spans: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_id, form_id, call_id = uuid4(), uuid4(), uuid4()
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("job_entrypoint") as worker_span:
            traceparent = current_traceparent()
            worker_trace_id = worker_span.get_span_context().trace_id
        assert traceparent is not None

        store = TraceLinkStore(FakeTraceLinkRedis())
        await store.publish(room_name_for_call(tenant_id, call_id), traceparent)

        consumer = _consumer(monkeypatch, trace_links=store)
        await consumer._process_job(
            PostCallJob(tenant_id=tenant_id, form_id=form_id, call_id=call_id)
        )

        span = next(s for s in otel_spans.get_finished_spans() if s.name == "vera.post_call.eval")
        assert span.context.trace_id == worker_trace_id


def _consumer(monkeypatch: pytest.MonkeyPatch, *, trace_links: TraceLinkStore) -> Any:
    """A PostCallConsumer whose eval is stubbed out — the span, not the eval, is
    what these tests are about."""

    async def _no_turns(*_args: Any, **_kwargs: Any) -> list[TranscriptTurn]:
        return []

    @asynccontextmanager
    async def _session(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    async def _evaluate(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            status=SimpleNamespace(value="done"),
            answers_written=0,
            reviewed_fields=[],
            transitioned=True,
        )

    async def _no_dispatch(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(post_call_consumer, "build_turns", _no_turns)
    monkeypatch.setattr(post_call_consumer, "tenant_session", _session)
    monkeypatch.setattr(post_call_consumer, "evaluate_call", _evaluate)
    monkeypatch.setattr(post_call_consumer, "run_dispatch_pass", _no_dispatch)
    return PostCallConsumer(
        redis=None,  # type: ignore[arg-type]
        sessionmaker=None,  # type: ignore[arg-type]
        call_stream=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        livekit=None,
        trace_links=trace_links,
    )
