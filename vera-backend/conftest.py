"""Root conftest for vera-backend — ensures session-scoped fixtures run earliest."""

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


@pytest.fixture(scope="session", autouse=True)
def _install_test_tracer_provider() -> InMemorySpanExporter:
    # Session-scoped + autouse at the root conftest level: pytest instantiates this
    # before the FIRST test in the entire session runs, regardless of which testpath
    # or conftest file it comes from (winning the global set_tracer_provider one-shot
    # race before test_otel_auth.py or any other test can set a different provider).
    return install_test_tracer_provider()


@pytest.fixture
def otel_spans(
    _install_test_tracer_provider: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    _install_test_tracer_provider.clear()
    yield _install_test_tracer_provider
    _install_test_tracer_provider.clear()
