from typing import Any
from uuid import uuid4

import pytest

from control_plane import post_call_consumer
from control_plane.call_summary import TranscriptTurn as StreamTurn
from control_plane.post_call_consumer import PostCallConsumer, build_turns
from vera_core.events import PostCallJob
from vera_core.integrations.llm import TranscriptTurn
from vera_core.models.enums import FormStatus
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
