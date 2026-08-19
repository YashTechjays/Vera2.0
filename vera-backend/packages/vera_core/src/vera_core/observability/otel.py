"""OTel -> self-hosted Langfuse wiring.

Langfuse ingests OTLP traces at /api/public/otel/v1/traces. This module owns the
TracerProvider setup for BOTH processes; when no Langfuse host is configured
(local dev, CI) tracing stays a no-op and code paths that create spans still
work against the default no-op tracer.

Authorization: Basic <base64(public_key:secret_key)> is computed from
VERA_LANGFUSE_PUBLIC_KEY / VERA_LANGFUSE_SECRET_KEY when both are set.
When either key is absent the exporter is created without an Authorization
header (Langfuse will return 401; useful for local dev without auth).

TODO(vera-2.x): sampling config and the voice-pipeline span instrumentation
(STT/LLM/TTS stages tagged with call_trace_attributes).
"""

import base64
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

    from vera_core.observability.llm_usage_export import UsageEnrichingExporter

    headers: dict[str, str] | None = None
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        token = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {token}"}

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
        headers=headers,
    )
    # Corrects the SDK's llm_request spans so Gemini cache hits are not billed at the
    # full input rate (see llm_usage_export). Every other span passes through untouched.
    provider.add_span_processor(BatchSpanProcessor(UsageEnrichingExporter(exporter)))
    trace.set_tracer_provider(provider)
    logger.info("observability: exporting OTLP traces to %s", settings.langfuse_host)
    return provider
