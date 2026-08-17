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


def _metrics(prompt: int, cached: int, completion: int, total: int | None = None) -> str:
    """`total` mirrors the provider's own total_token_count. Omitted means the blob
    carries none, which must leave the derived thinking count at zero."""
    blob = {
        "prompt_tokens": prompt,
        "prompt_cached_tokens": cached,
        "completion_tokens": completion,
    }
    if total is not None:
        blob["total_tokens"] = total
    return json.dumps(blob)


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
        self.flush_timeout: int | None = None

    def export(self, spans: Any) -> Any:
        self.exported.extend(spans)
        return None

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushed = True
        self.flush_timeout = timeout_millis
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
        exporter.shutdown()
        assert inner.shutdown_called

    def test_force_flush_forwards_its_timeout_to_the_wrapped_exporter(self) -> None:
        # A non-default value: forwarding the default would coincidentally match the
        # fake's own default and hide a regression that drops the argument entirely.
        inner = _RecordingExporter()
        exporter = UsageEnrichingExporter(inner)
        exporter.force_flush(1234)
        assert inner.flushed
        assert inner.flush_timeout == 1234


class TestCacheVisibility:
    """usage_details already carries the cached count, but reading it requires
    comparing against `input` by hand. These attributes make cache behaviour
    sortable/filterable, so a prompt change that breaks the cacheable prefix is
    visible rather than just quietly more expensive."""

    def test_cache_attributes_report_the_hit_ratio(self) -> None:
        inner = _RecordingExporter()
        # 9360 of 12480 prompt tokens were hits -> 0.75
        UsageEnrichingExporter(inner).export(
            [_span({"lk.llm_metrics": _metrics(12480, 9360, 210)})]
        )
        attrs = inner.exported[0].attributes
        assert attrs["vera.llm.cached_tokens"] == 9360
        assert attrs["vera.llm.cache_hit_ratio"] == 0.75

    def test_a_measured_zero_is_reported_not_omitted(self) -> None:
        # 0.0 must be distinguishable from "unknown": this request genuinely had no
        # cache hits, which is a fact worth seeing, not an absence of data.
        inner = _RecordingExporter()
        UsageEnrichingExporter(inner).export([_span({"lk.llm_metrics": _metrics(500, 0, 20)})])
        attrs = inner.exported[0].attributes
        assert attrs["vera.llm.cached_tokens"] == 0
        assert attrs["vera.llm.cache_hit_ratio"] == 0.0

    def test_an_unusable_blob_reports_neither_attribute(self) -> None:
        # The other half of that contract: absent means "unknown", so a malformed blob
        # must not masquerade as a measured zero.
        inner = _RecordingExporter()
        UsageEnrichingExporter(inner).export([_span({"lk.llm_metrics": "not json"})])
        attrs = inner.exported[0].attributes
        assert "vera.llm.cached_tokens" not in attrs
        assert "vera.llm.cache_hit_ratio" not in attrs

    def test_a_promptless_request_reports_neither_attribute(self) -> None:
        # No prompt tokens means nothing to say about caching — better absent than 0/0.
        inner = _RecordingExporter()
        UsageEnrichingExporter(inner).export([_span({"lk.llm_metrics": _metrics(0, 0, 12)})])
        attrs = inner.exported[0].attributes
        assert "vera.llm.cache_hit_ratio" not in attrs

    def test_the_cache_attributes_are_not_priced(self) -> None:
        # They must stay OUT of usage_details, or Langfuse would try to price them
        # against a model entry that has no such key.
        inner = _RecordingExporter()
        UsageEnrichingExporter(inner).export(
            [_span({"lk.llm_metrics": _metrics(12480, 9360, 210)})]
        )
        usage = json.loads(inner.exported[0].attributes["langfuse.observation.usage_details"])
        assert set(usage) == {"input", "cached", "output"}


class TestThinkingTokens:
    """Gemini bills thinking as OUTPUT, but the Google plugin sets
    `completion_tokens = candidates_token_count`, which EXCLUDES thoughts, while
    `total_tokens` includes them. Reading completion alone understates output on any
    thinking-enabled model — and Vera configures thinking per tenant. The post-call
    eval path reads `thoughts_token_count` directly, so these keep them in agreement."""

    def test_thinking_tokens_are_folded_into_output(self) -> None:
        # prompt 1000 (300 cached) + completion 40 + 260 thoughts -> total 1300
        usage = corrected_usage_details(_metrics(1000, 300, 40, total=1300))
        assert usage == {"input": 700, "cached": 300, "output": 300}

    def test_usage_reconciles_against_the_providers_own_total(self) -> None:
        # The strongest invariant available: nothing billed is invented or dropped.
        total = 1300
        usage = corrected_usage_details(_metrics(1000, 300, 40, total=total))
        assert usage is not None
        assert usage["input"] + usage["cached"] + usage["output"] == total

    def test_no_thinking_leaves_output_untouched(self) -> None:
        # total == prompt + completion, so the residual is zero.
        usage = corrected_usage_details(_metrics(1000, 300, 40, total=1040))
        assert usage == {"input": 700, "cached": 300, "output": 40}

    def test_an_sdk_that_starts_including_thoughts_does_not_double_count(self) -> None:
        # Self-correcting: if completion_tokens ever grows to include thoughts, the
        # residual collapses to 0 rather than adding them a second time.
        usage = corrected_usage_details(_metrics(1000, 300, 300, total=1300))
        assert usage == {"input": 700, "cached": 300, "output": 300}

    def test_a_missing_total_cannot_subtract_usage(self) -> None:
        # A provider reporting no total yields a negative residual; clamping keeps
        # output at the reported completion instead of eroding it.
        assert corrected_usage_details(_metrics(1000, 300, 40)) == {
            "input": 700,
            "cached": 300,
            "output": 40,
        }
