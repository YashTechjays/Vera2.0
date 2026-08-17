"""A wedged Redis must never stall call setup: the trace-link publish is best-effort
and time-boxed, so a hang degrades this call to session-only correlation rather than
delaying the greeting."""

import asyncio

import pytest

from agent_worker.main import _TRACE_LINK_PUBLISH_TIMEOUT_S, _publish_trace_link

_TRACEPARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


class _HangingRedis:
    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_a_hanging_publish_does_not_block_and_does_not_raise() -> None:
    hanging = _HangingRedis()
    await asyncio.wait_for(
        _publish_trace_link(
            hanging,  # type: ignore[arg-type]
            "call--t--c",
            _TRACEPARENT,
            timeout_s=0.05,
        ),
        timeout=1.0,
    )


def test_default_publish_budget_is_short() -> None:
    # A short, fixed budget — not the general Redis timeout — is what keeps this
    # off the greeting's critical path.
    assert _TRACE_LINK_PUBLISH_TIMEOUT_S == 2.0
