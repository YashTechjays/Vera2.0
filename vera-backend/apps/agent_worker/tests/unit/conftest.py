"""Shared helpers for the agent_worker unit tests."""

from collections.abc import Iterator

import pytest
from livekit.agents import Agent, llm
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


def ctx_texts(ctx: llm.ChatContext) -> list[str]:
    """The plain-string message contents of a chat context, in order."""
    return [
        content
        for item in ctx.items
        if item.type == "message"
        for content in item.content
        if isinstance(content, str)
    ]


def chat_ctx_texts(agent: Agent) -> list[str]:
    """An agent's own context texts — the turns a handoff must carry forward."""
    return ctx_texts(agent.chat_ctx)


def pytest_configure(config: pytest.Config) -> None:
    # Runs for every loaded conftest at collection-start, before ANY fixture (including an
    # integration test's app-boot fixture that might call the REAL configure_observability()
    # if VERA_LANGFUSE_HOST happens to be set) — this is what actually guarantees winning the
    # global set_tracer_provider one-shot race, which a session-scoped autouse fixture alone
    # does not (a fixture only sets up when its first consuming test is about to run, which
    # can already be too late).
    install_test_tracer_provider()


@pytest.fixture
def otel_spans() -> Iterator[InMemorySpanExporter]:
    """Cleared before and after each test; every test gets a clean span list even
    though the underlying TracerProvider is process-global and installed once."""
    exporter = install_test_tracer_provider()
    exporter.clear()
    yield exporter
    exporter.clear()
