"""Correct the SDK's LLM spans so cache hits are not billed at the full input rate.

The Google plugin reports `cached_content_token_count` and livekit-agents carries it
as `LLMMetrics.prompt_cached_tokens`, but the `llm_request` span sets only
`gen_ai.usage.input_tokens` / `output_tokens` — and `prompt_tokens` INCLUDES the
cached ones. Langfuse therefore prices every cache hit at the full input rate, so
Vera's LLM cost is overstated in proportion to the hit rate. The voice cascade
re-sends a growing chat context every turn, which is the archetypal implicit-cache
workload, so the error is worst on the longest calls.

The fix cannot be a Vera-owned span: a second generation for the same request would be
summed by Langfuse and double-count it. It must correct the SAME span. Two properties
make that exact:

- the span already carries everything needed, in `lk.llm_metrics`;
- `langfuse.*` attributes take precedence over `gen_ai.*`, so adding
  `langfuse.observation.usage_details` overrides the SDK's uncorrected counts.

Additive and keyed on one attribute: a span without `lk.llm_metrics` is untouched, and
a malformed or renamed blob exports the ORIGINAL span. Losing a span is worse than
exporting an uncorrected one.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from vera_core.observability.usage_spans import (
    GENERATION,
    OBSERVATION_TYPE_ATTR,
    USAGE_CACHED,
    USAGE_DETAILS_ATTR,
    USAGE_INPUT,
    USAGE_OUTPUT,
)

logger = logging.getLogger("vera.observability")

# The SDK's own metrics blob (livekit.agents.telemetry.trace_types.ATTR_LLM_METRICS).
LLM_METRICS_ATTR = "lk.llm_metrics"


def corrected_usage_details(raw_metrics: str) -> dict[str, int] | None:
    """Usage keys for one `lk.llm_metrics` blob, or None when it is unusable.

    `input` is reduced by the cached count because `prompt_tokens` includes it —
    sending both whole would double-count the hits. `input + cached` therefore always
    reconstructs the SDK's original `prompt_tokens`.
    """
    try:
        metrics = json.loads(raw_metrics)
    except (TypeError, ValueError):
        return None
    if not isinstance(metrics, dict):
        return None
    prompt = int(metrics.get("prompt_tokens", 0) or 0)
    cached = int(metrics.get("prompt_cached_tokens", 0) or 0)
    completion = int(metrics.get("completion_tokens", 0) or 0)
    details: dict[str, int] = {}
    if prompt - cached:
        details[USAGE_INPUT] = prompt - cached
    if cached:
        details[USAGE_CACHED] = cached
    if completion:
        details[USAGE_OUTPUT] = completion
    return details or None


def _enrich(span: ReadableSpan) -> ReadableSpan:
    attributes = span.attributes or {}
    raw = attributes.get(LLM_METRICS_ATTR)
    if not isinstance(raw, str):
        return span
    usage = corrected_usage_details(raw)
    if usage is None:
        return span
    # ReadableSpan exposes attributes read-only, so re-project rather than mutate.
    merged = dict(attributes)
    merged[USAGE_DETAILS_ATTR] = json.dumps(usage)
    merged[OBSERVATION_TYPE_ATTR] = GENERATION
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=merged,
        events=span.events,
        links=span.links,
        status=span.status,
        kind=span.kind,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class UsageEnrichingExporter(SpanExporter):
    """Wraps a SpanExporter, correcting LLM spans' usage on the way out."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        enriched: list[ReadableSpan] = []
        for span in spans:
            try:
                enriched.append(_enrich(span))
            except Exception as exc:
                # Never drop a span over a correction failure.
                logger.warning("llm usage enrichment failed: %s", type(exc).__name__)
                enriched.append(span)
        result: SpanExportResult = self._wrapped.export(enriched)
        return result

    def shutdown(self) -> None:
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        flushed: bool = self._wrapped.force_flush(timeout_millis)
        return flushed
