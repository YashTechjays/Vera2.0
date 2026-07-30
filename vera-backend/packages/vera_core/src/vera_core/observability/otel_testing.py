"""Test-only OTel tracer provider installer plus the shared PHI-denylist span assertion.

`opentelemetry.trace.set_tracer_provider()` is a process-global, one-shot call — the first
call anywhere in the process wins; every call after that is silently ignored (with a warning),
including this file's own on a second invocation. This repo's test suite runs
`apps/agent_worker/tests` and `tests/` in one pytest session (`pyproject.toml` testpaths) whose
`conftest.py` files don't share fixtures, so both trees call `install_test_tracer_provider()`
from their own conftest; the module-level guard below makes that safe and gives both the SAME
exporter regardless of which tree calls first.

**Callers MUST invoke this from a `pytest_configure(config)` HOOK, not from a fixture** — see
the `conftest.py` in both trees. pytest runs `pytest_configure` for every loaded conftest at
collection-start, before ANY test or fixture executes, so the installer is guaranteed to reach
`set_tracer_provider()` first and win the one-shot race.

A fixture — *including* a session-scoped autouse one — is NOT sufficient, and this is not
theoretical: it was the bug fixed in commit `e0fd7a0c`. A session-scoped autouse fixture is
still lazy; it only sets up when its first consuming test in that tree is about to run. Any
test that boots the real app before then (an integration test whose app-boot fixture calls
`configure_observability()`, which installs the REAL Langfuse/OTLP provider when
`VERA_LANGFUSE_HOST` is set) reaches `set_tracer_provider()` first and permanently wins.
Our later call is then silently ignored, this exporter captures nothing, and every span
assertion in both trees fails with an empty span list — a failure mode that depends on
collection order and on the ambient environment, so it reproduces in CI and not locally.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
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


def assert_no_phi_values(span: ReadableSpan, *forbidden: str) -> None:
    """Fail if any `forbidden` value reached the exported span (design §8's per-span
    PHI denylist check). Lives here so both test trees share ONE implementation.

    Sweeps everything the exporter ships: the span name, every attribute value, the
    status description (which OTel fills with ``f"{type}: {exc}"`` unless the span was
    opened with ``set_status_on_exception=False``), and every event's name/attributes
    (populated unless ``record_exception=False``).

    The check is SUBSTRING, not equality: a PHI value embedded in a longer string
    (``"answer: No"``, ``"... bad metadata Jane Doe"``) discloses exactly as much as the
    bare value, and an ``!=`` comparison would wave it straight through.
    """
    haystack = [span.name, span.status.description or ""]
    haystack += [str(value) for value in (span.attributes or {}).values()]
    for event in span.events:
        haystack.append(event.name)
        haystack += [str(value) for value in (event.attributes or {}).values()]
    for needle in forbidden:
        for text in haystack:
            assert needle not in text, (
                f"PHI denylist: {needle!r} leaked into span {span.name!r} via {text!r}"
            )
