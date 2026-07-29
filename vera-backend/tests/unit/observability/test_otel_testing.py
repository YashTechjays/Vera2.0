"""The shared test-tracer-provider installer used by the otel_spans fixture."""

from opentelemetry import trace

from vera_core.observability.otel_testing import install_test_tracer_provider


def test_install_is_idempotent_and_captures_spans() -> None:
    exporter = install_test_tracer_provider()
    exporter.clear()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe"):
        pass
    names = [span.name for span in exporter.get_finished_spans()]
    assert "probe" in names

    # Calling again must return the SAME exporter (no second TracerProvider install)
    again = install_test_tracer_provider()
    assert again is exporter
