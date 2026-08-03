from typing import Any
from uuid import uuid4

import pytest

from control_plane import post_call_consumer
from control_plane.call_summary import TranscriptTurn as StreamTurn
from control_plane.post_call_consumer import build_turns
from vera_core.integrations.llm import TranscriptTurn


@pytest.mark.asyncio
async def test_build_turns_enumerates_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_turns adapts dev's (source, role, text) snapshot turns into the eval's
    seq-indexed TranscriptTurn, in order — seq is the evidence pointer."""

    async def fake_snapshot_turns(*_args: Any, **_kwargs: Any) -> list[StreamTurn]:
        return [
            StreamTurn(source="rep", role="user", text="hello"),
            StreamTurn(source="bot", role="agent", text="in network"),
        ]

    monkeypatch.setattr(post_call_consumer, "snapshot_turns", fake_snapshot_turns)

    turns = await build_turns(None, None, uuid4(), uuid4())  # type: ignore[arg-type]

    assert turns == [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
