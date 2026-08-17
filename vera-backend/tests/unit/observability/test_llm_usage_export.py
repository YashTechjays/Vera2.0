"""The SDK measures LLM cache hits and then discards them (design §2.6): the Google
plugin sets prompt_cached_tokens, LLMMetrics carries it, and llm_request sets only
input/output — so Langfuse prices cache hits at the full input rate and every LLM
figure is overstated in proportion to hit rate."""

import json
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from vera_core.observability.llm_usage_export import (
    UsageEnrichingExporter,
    corrected_usage_details,
)


def _metrics(prompt: int, cached: int, completion: int) -> str:
    return json.dumps(
        {
            "prompt_tokens": prompt,
            "prompt_cached_tokens": cached,
            "completion_tokens": completion,
        }
    )


class TestCorrectedUsage:
    def test_cached_tokens_are_split_out_of_input(self) -> None:
        assert corrected_usage_details(_metrics(12480, 9360, 210)) == {
            "input": 3120,
            "cached": 9360,
            "output": 210,
        }

    def test_input_plus_cached_reconstructs_the_original_prompt_count(self) -> None:
        # THE invariant. Drop the subtraction and cached is double-counted; drop the
        # cached key while still subtracting and the hits vanish. Both are silent.
        usage = corrected_usage_details(_metrics(12480, 9360, 210))
        assert usage is not None
        assert usage["input"] + usage["cached"] == 12480

    def test_no_cache_hits_omits_the_cached_key(self) -> None:
        assert corrected_usage_details(_metrics(500, 0, 20)) == {"input": 500, "output": 20}

    def test_a_malformed_blob_yields_none(self) -> None:
        assert corrected_usage_details("not json") is None
        assert corrected_usage_details("[]") is None

    def test_every_value_is_an_int(self) -> None:
        usage = corrected_usage_details(_metrics(12480, 9360, 210))
        assert usage is not None
        assert all(isinstance(v, int) for v in usage.values())


class _RecordingExporter:
    def __init__(self) -> None:
        self.exported: list[Any] = []
        self.shutdown_called = False
        self.flushed = False

    def export(self, spans: Any) -> Any:
        self.exported.extend(spans)
        return None

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushed = True
        return True


def _span(attributes: dict[str, Any]) -> ReadableSpan:
    """A REAL ReadableSpan — the enrichment path re-projects one, reading .parent,
    .resource, .events, .status and friends, so a duck-typed fake with only
    .attributes would fall into the exception fallback and silently pass through."""
    return ReadableSpan(name="llm_request", attributes=attributes)


class TestExporter:
    def test_an_llm_span_gains_corrected_usage_details(self) -> None:
        inner = _RecordingExporter()
        span = _span({"lk.llm_metrics": _metrics(12480, 9360, 210)})
        UsageEnrichingExporter(inner).export([span])
        attrs = inner.exported[0].attributes
        assert json.loads(attrs["langfuse.observation.usage_details"]) == {
            "input": 3120,
            "cached": 9360,
            "output": 210,
        }
        assert attrs["langfuse.observation.type"] == "generation"

    def test_a_span_without_llm_metrics_passes_through_untouched(self) -> None:
        inner = _RecordingExporter()
        span = _span({"vera.room": "call--a--b"})
        UsageEnrichingExporter(inner).export([span])
        assert inner.exported == [span]

    def test_a_malformed_blob_exports_the_original_span(self) -> None:
        # An SDK upgrade renaming or reshaping lk.llm_metrics must degrade to today's
        # behavior. Losing a span is worse than exporting an uncorrected one.
        inner = _RecordingExporter()
        span = _span({"lk.llm_metrics": "not json"})
        UsageEnrichingExporter(inner).export([span])
        assert inner.exported == [span]

    def test_lifecycle_calls_are_delegated(self) -> None:
        # Without delegation, spans queued at process exit are silently lost.
        inner = _RecordingExporter()
        exporter = UsageEnrichingExporter(inner)
        exporter.force_flush()
        exporter.shutdown()
        assert inner.flushed and inner.shutdown_called
