"""The RETRY-call focus overlay (rides PlanRunController's extra_instructions)."""

from __future__ import annotations

from agent_worker.prompt import retry_focus_block


def test_retry_focus_names_only_the_missing_fields() -> None:
    block = retry_focus_block(["Doctor Inside Network", "Copay Amount"])
    assert block.startswith("RETRY CALL.")
    assert "Doctor Inside Network, Copay Amount" in block
    assert "Do not re-verify anything else." in block


def test_retry_focus_single_field() -> None:
    block = retry_focus_block(["Plan Fund Type"])
    assert "collect ONLY the following still-missing data points" in block
    assert "Plan Fund Type" in block
