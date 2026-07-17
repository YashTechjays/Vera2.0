"""Audience filtering + keepalive framing for the notification SSE (the pure
generator; auth/permission gating rides the same chain as every endpoint and is
exercised at boot)."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from control_plane.api.v1.notifications import delivers_to, notification_frames
from control_plane.sse import SSE_KEEPALIVE_FRAME
from vera_core.notifications import Notification, NotificationAudience


def _n(audience: NotificationAudience) -> Notification:
    return Notification(
        type="intervention_needed",
        audience=audience,
        data={"call_id": "c", "score": 30, "flag": "other", "reason": "r"},
        ts=1,
    )


def test_delivers_to() -> None:
    me, other = uuid4(), uuid4()
    assert delivers_to(NotificationAudience(kind="tenant"), me)
    assert delivers_to(NotificationAudience(kind="user", user_id=str(me)), me)
    assert not delivers_to(NotificationAudience(kind="user", user_id=str(other)), me)


@pytest.mark.asyncio
async def test_notification_frames_filters_and_keeps_alive() -> None:
    me, other = uuid4(), uuid4()

    async def items() -> AsyncIterator[tuple[str, Notification] | None]:
        yield None  # idle tick -> keepalive comment
        yield "1-1", _n(NotificationAudience(kind="user", user_id=str(other)))  # filtered
        yield "1-2", _n(NotificationAudience(kind="tenant"))

    frames = [frame async for frame in notification_frames(items(), user_id=me)]
    assert frames[0] == SSE_KEEPALIVE_FRAME
    assert len(frames) == 2  # the other-user notification never leaves the server
    assert frames[1].startswith("id: 1-2\ndata: ")
    assert '"tenant"' in frames[1]
