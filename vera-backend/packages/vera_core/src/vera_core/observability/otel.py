"""OTel -> self-hosted Langfuse wiring.

Langfuse ingests OTLP traces at /api/public/otel. This module owns the
TracerProvider setup for BOTH processes; when no Langfuse host is configured
(local dev, CI) tracing stays a no-op and code paths that create spans still
work against the default no-op tracer.

TODO(vera-2.x): Langfuse public/secret key auth header from SecretProvider,
sampling config, and the voice-pipeline span instrumentation (STT/LLM/TTS
stages tagged with call_trace_attributes).
"""

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from vera_core.config import Settings

logger = logging.getLogger("vera.observability")


def configure_observability(settings: Settings) -> TracerProvider | None:
    """Install a TracerProvider exporting to Langfuse; None => no-op tracing."""
    if not settings.langfuse_host:
        logger.info("observability disabled (no VERA_LANGFUSE_HOST)")
        return None

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    exporter = OTLPSpanExporter(endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("observability: exporting OTLP traces to %s", settings.langfuse_host)
    return provider
