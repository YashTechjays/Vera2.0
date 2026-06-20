"""Unit tests for OTLP Basic-auth header wiring in configure_observability."""

import base64
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry import trace

from vera_core.config.settings import Settings
from vera_core.observability.otel import configure_observability


@pytest.fixture(autouse=True)
def reset_tracer_provider() -> Generator[None, None, None]:
    """Reset the global OTel tracer provider between tests."""
    yield
    trace.set_tracer_provider(trace.ProxyTracerProvider())


def _make_settings(
    langfuse_host: str | None = None,
    langfuse_public_key: str | None = None,
    langfuse_secret_key: str | None = None,
    otel_service_name: str = "vera-test",
) -> Settings:
    """Build a Settings with only the observability fields set (ignore env)."""
    return Settings.model_construct(
        langfuse_host=langfuse_host,
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
        otel_service_name=otel_service_name,
    )


def test_no_host_returns_none() -> None:
    settings = _make_settings()
    result = configure_observability(settings)
    assert result is None


def test_with_keys_sends_basic_auth_header() -> None:
    settings = _make_settings(
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk-lf-test-public",
        langfuse_secret_key="sk-lf-test-secret",
    )
    captured: dict[str, object] = {}

    def fake_exporter(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    with patch(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        side_effect=fake_exporter,
    ):
        provider = configure_observability(settings)

    assert provider is not None
    expected_token = base64.b64encode(b"pk-lf-test-public:sk-lf-test-secret").decode()
    headers = captured.get("headers")
    assert isinstance(headers, dict)
    assert headers["Authorization"] == f"Basic {expected_token}"


def test_without_keys_no_auth_header() -> None:
    settings = _make_settings(langfuse_host="http://localhost:3000")
    captured: dict[str, object] = {}

    def fake_exporter(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    with patch(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        side_effect=fake_exporter,
    ):
        configure_observability(settings)

    assert captured.get("headers") is None


def test_partial_keys_no_auth_header() -> None:
    """Only public key set — should not send auth (secret missing)."""
    settings = _make_settings(
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk-lf-test-public",
        # langfuse_secret_key intentionally absent
    )
    captured: dict[str, object] = {}

    def fake_exporter(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    with patch(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        side_effect=fake_exporter,
    ):
        configure_observability(settings)

    assert captured.get("headers") is None
