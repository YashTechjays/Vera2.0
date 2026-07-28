"""Shared fixtures for tests/unit/ (vera_core + control_plane)."""

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


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
    exporter = install_test_tracer_provider()
    exporter.clear()
    yield exporter
    exporter.clear()
