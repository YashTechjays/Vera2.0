"""The pure logic behind `just langfuse-verify`.

The script's value is that it FAILS on a real problem, so the parts that decide
pass/fail are worth testing: what counts as billable, and what counts as reconciled.
"""

from typing import Any

from scripts.verify_langfuse_traces import reconciles


def _obs(usage: dict[str, int] | None, reported_input: int | None) -> dict[str, Any]:
    attrs = {} if reported_input is None else {"gen_ai.usage.input_tokens": reported_input}
    return {"usageDetails": usage, "metadata": {"attributes": attrs}}


class TestReconciles:
    def test_a_correct_cache_split_reconciles(self) -> None:
        # 2932 uncached + 1961 cached is the 4893 the provider actually charged for.
        assert reconciles(_obs({"input": 2932, "cached": 1961}, 4893)) is True

    def test_a_double_counted_split_does_not_reconcile(self) -> None:
        # Dropping the subtraction: input still carries the cached tokens, so the
        # sum overshoots the provider's own count. This is the regression that would
        # otherwise be invisible — the numbers stay individually plausible.
        assert reconciles(_obs({"input": 4893, "cached": 1961}, 4893)) is False

    def test_vanished_cache_tokens_do_not_reconcile(self) -> None:
        # The opposite error: input reduced but the cached key omitted entirely.
        assert reconciles(_obs({"input": 2932}, 4893)) is False

    def test_a_span_with_no_sdk_count_is_unverifiable_not_wrong(self) -> None:
        # Vera's own STT/TTS generations have no gen_ai.usage.input_tokens to compare
        # against. Those must read as "nothing to check", never as a failure.
        assert reconciles(_obs({"stt_audio_ms": 5000}, None)) is None

    def test_a_span_with_no_usage_is_unverifiable(self) -> None:
        assert reconciles(_obs(None, 4893)) is None
