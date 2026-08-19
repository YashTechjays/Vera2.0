"""Fixtures shared by BOTH test trees (tests/unit and tests/integration).

`pytest_configure` lives here rather than in `tests/unit/conftest.py` so it also runs
when only the integration tree is collected — the race it guards is precisely an
integration app-boot fixture reaching the real `configure_observability()`.
"""

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


class FakeTraceLinkRedis:
    """The get/set surface `TraceLinkStore` uses. One copy, because three test modules
    stubbing the same client drifted on whether `get` returns str or bytes — the two
    the store is written to accept."""

    def __init__(self, *, fails: bool = False, returns_bytes: bool = True) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._fails = fails
        self._returns_bytes = returns_bytes

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self._fails:
            raise ConnectionError("redis down")
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> bytes | str | None:
        if self._fails:
            raise ConnectionError("redis down")
        value = self.values.get(key)
        if value is None:
            return None
        return value.encode() if self._returns_bytes else value
