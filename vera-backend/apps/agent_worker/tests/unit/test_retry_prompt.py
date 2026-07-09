"""Tests for retry_fields partial-prompt nudge."""

from __future__ import annotations

from agent_worker.prompt import build_instructions


def test_retry_fields_prepends_focus_block() -> None:
    out = build_instructions(None, retry_fields=["Network status", "Specialist copay"])
    assert "RETRY" in out.upper()
    assert "Network status" in out and "Specialist copay" in out
    # base script still present
    assert "verifying insurance coverage" in out.lower()


def test_no_retry_fields_is_unchanged() -> None:
    assert build_instructions(None, retry_fields=None) == build_instructions(None)
    assert build_instructions(None, retry_fields=[]) == build_instructions(None)
