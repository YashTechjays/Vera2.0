"""POST /calls/{call_id}/transcribe — the whisper path's trace wiring.

This endpoint had no test of any kind. Two things about it fail silently, and neither
shows up in the HTTP response, the audit rows, or a unit test of the pieces:

1. the endpoint must hand `call_scoped_span` the store from `get_trace_link_store`, or
   whisper's Deepgram spend forms its own trace and drops out of the call's total;
2. the nested `vera.stt.usage` generation must inherit that span through AMBIENT
   context. Whisper is the one attach site that deliberately passes no
   `parent_context` (`vera_core/stt.py`), on the grounds that livekit creates the
   metrics task inside `transcribe()` (`stt/stt.py:377`) so it captures the request's
   context. The stub below reproduces exactly that shape — a task created inside
   `transcribe` emitting through the real `attach_usage_spans` — so the COMPOSITION is
   locked in. It does not replace a live call: only that proves livekit still creates
   the task where we think it does.
"""

import asyncio
from collections.abc import Generator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from livekit.agents.metrics import STTMetrics
from livekit.agents.metrics.base import Metadata
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeTraceLinkRedis
from tests.integration.control_plane.conftest import RBACWorld, seed_call
from tests.integration.control_plane.test_calls import _auth, seeded_form_id  # noqa: F401
from vera_core.observability import TraceLinkStore, attach_usage_spans
from vera_core.observability.correlation import room_name_for_call
from vera_core.observability.trace_link import current_traceparent
from vera_core.observability.usage_spans import SPAN_STT_USAGE


class _Emitter:
    """The `metrics_collected` surface `attach_usage_spans` listens on."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        self._handlers.append(handler)

    def emit(self, metrics: Any) -> None:
        for handler in self._handlers:
            handler(metrics)


class _StubWhisperSTT:
    """Mirrors ResilientSTT's shape where it matters: the usage listener is attached at
    chain-build time with NO captured parent, and the metrics task is created inside
    `transcribe()` so it inherits whatever span is current there."""

    def __init__(self, text: str = "check the deductible") -> None:
        self.text = text
        self._emitter = _Emitter()
        attach_usage_spans(self._emitter)  # no parent_context — ambient, as whisper does

    async def transcribe(self, audio: bytes, *, mime_type: str) -> str:
        async def _emit_metrics() -> None:
            self._emitter.emit(
                STTMetrics(
                    request_id="req-1",
                    timestamp=1.0,
                    duration=0.0,
                    label="deepgram.STT",
                    audio_duration=3.5,
                    streamed=True,
                    metadata=Metadata(model_name="flux-general-en", model_provider="Deepgram"),
                )
            )

        await asyncio.create_task(_emit_metrics())
        return self.text


@pytest.fixture
def stub_whisper(authz_app: FastAPI) -> Generator[_StubWhisperSTT]:
    stub = _StubWhisperSTT()
    prior = getattr(authz_app.state, "whisper_stt", None)
    authz_app.state.whisper_stt = stub
    yield stub
    authz_app.state.whisper_stt = prior


@pytest.mark.asyncio
async def test_whisper_and_its_usage_span_both_land_in_the_calls_trace(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    authz_app: FastAPI,
    stub_whisper: _StubWhisperSTT,
    otel_spans: InMemorySpanExporter,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)

    store = TraceLinkStore(FakeTraceLinkRedis())
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("job_entrypoint") as worker_span:
        traceparent = current_traceparent()
        worker_trace_id = worker_span.get_span_context().trace_id
    assert traceparent is not None
    await store.publish(room, traceparent)

    prior = authz_app.state.trace_link_store
    authz_app.state.trace_link_store = store
    otel_spans.clear()
    try:
        resp = await client.post(
            f"/api/v1/calls/{call_id}/on-demand-transcribe",
            headers=_auth(rbac_world.admin_token),
            files={"audio": ("clip.webm", b"\x00\x01\x02fake-opus", "audio/webm")},
        )
    finally:
        authz_app.state.trace_link_store = prior

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["text"] == "check the deductible"

    finished = otel_spans.get_finished_spans()
    whisper = [s for s in finished if s.name == "vera.coaching.whisper"]
    assert len(whisper) == 1, "the endpoint opened no vera.coaching.whisper span"
    assert whisper[0].context.trace_id == worker_trace_id, (
        "vera.coaching.whisper formed its own trace — the endpoint is not resolving "
        "the trace link, so whisper spend drops out of the call's total"
    )

    usage = [s for s in finished if s.name == SPAN_STT_USAGE]
    assert len(usage) == 1, "no vera.stt.usage generation — whisper STT is unpriced"
    assert usage[0].context.trace_id == worker_trace_id, (
        "the usage generation escaped to its own trace: ambient context did not carry "
        "into the metrics task, so Deepgram's whisper cost is invisible per-call"
    )
    assert usage[0].parent is not None
    assert usage[0].parent.span_id == whisper[0].context.span_id, (
        "the usage generation is not nested under vera.coaching.whisper"
    )
