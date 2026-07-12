"""SSE framing over a keepalive-aware stream (control_plane.sse)."""

from collections.abc import AsyncIterator

import pytest

from control_plane.sse import SSE_KEEPALIVE_FRAME, frames_with_keepalive


def _frame(entry_id: str, event: str) -> str:
    return f"id: {entry_id}\ndata: {event}\n\n"


async def _items(
    *items: tuple[str, str] | None,
) -> AsyncIterator[tuple[str, str] | None]:
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_events_are_framed_and_keepalive_ticks_become_comments() -> None:
    got = [
        frame
        async for frame in frames_with_keepalive(
            _items(("1-0", "hello"), None, ("2-0", "world"), None), _frame
        )
    ]
    assert got == [
        "id: 1-0\ndata: hello\n\n",
        SSE_KEEPALIVE_FRAME,
        "id: 2-0\ndata: world\n\n",
        SSE_KEEPALIVE_FRAME,
    ]


def test_keepalive_frame_is_an_sse_comment() -> None:
    # A comment frame (leading colon) is ignored by EventSource clients; it exists
    # only to keep bytes flowing through proxy read timeouts.
    assert SSE_KEEPALIVE_FRAME.startswith(":")
    assert SSE_KEEPALIVE_FRAME.endswith("\n\n")
