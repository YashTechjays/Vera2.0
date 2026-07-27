"""Test-only OTel tracer provider installer.

`opentelemetry.trace.set_tracer_provider()` is a process-global, one-shot call — the first
call anywhere in the process wins; every call after that is silently ignored (with a warning),
including this file's own on a second invocation. This repo's test suite runs
`apps/agent_worker/tests` and `tests/` in one pytest session (`pyproject.toml` testpaths) whose
`conftest.py` files don't share fixtures, so both trees call `install_test_tracer_provider()`
from their own conftest; the module-level guard below makes that safe and gives both the SAME
exporter regardless of which tree's fixture runs first. Callers MUST invoke this from a
session-scoped autouse fixture (see conftest.py in both trees) rather than a plain per-test
fixture — otherwise an unrelated test that calls the real `configure_observability()` (e.g.
`tests/unit/observability/test_otel_auth.py`) could win the one-shot race first if it happens
to run before ours, silently discarding every span this exporter would otherwise have captured.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter = InMemorySpanExporter()
_installed = False


def install_test_tracer_provider() -> InMemorySpanExporter:
    """Install the shared test TracerProvider on first call; a no-op (returning the
    same exporter) on every call after that."""
    global _installed
    if not _installed:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
        _installed = True
    return _exporter
