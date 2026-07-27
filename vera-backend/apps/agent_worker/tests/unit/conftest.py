"""Shared helpers for the agent_worker unit tests."""

from collections.abc import Iterator

import pytest
from livekit.agents import Agent
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


def chat_ctx_texts(agent: Agent) -> list[str]:
    """The plain-string message contents of an agent's chat_ctx, in order — the
    turns a handoff must carry forward (used to assert history is preserved)."""
    return [
        content
        for item in agent.chat_ctx.items
        if item.type == "message"
        for content in item.content
        if isinstance(content, str)
    ]


@pytest.fixture(scope="session", autouse=True)
def _install_test_tracer_provider() -> InMemorySpanExporter:
    # session-scoped + autouse: pytest instantiates this before the FIRST test in the whole
    # session runs, regardless of which file that is — see otel_testing.py's docstring for why
    # that matters (winning the global set_tracer_provider one-shot race).
    return install_test_tracer_provider()


@pytest.fixture
def otel_spans(
    _install_test_tracer_provider: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    """Cleared before and after each test; every test gets a clean span list even
    though the underlying TracerProvider is process-global and installed once."""
    _install_test_tracer_provider.clear()
    yield _install_test_tracer_provider
    _install_test_tracer_provider.clear()
