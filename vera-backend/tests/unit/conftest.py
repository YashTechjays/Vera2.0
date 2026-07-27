"""Shared fixtures for tests/unit/ (vera_core + control_plane)."""

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


@pytest.fixture(scope="session", autouse=True)
def _install_test_tracer_provider() -> InMemorySpanExporter:
    return install_test_tracer_provider()


@pytest.fixture
def otel_spans(
    _install_test_tracer_provider: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    _install_test_tracer_provider.clear()
    yield _install_test_tracer_provider
    _install_test_tracer_provider.clear()
