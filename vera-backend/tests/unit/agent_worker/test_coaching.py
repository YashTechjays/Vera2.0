"""CoachingListener: tails the call-event stream for coaching/whisper turns and
folds each into the currently active agent's chat context — never forcing an
immediate reply, always targeting session.current_agent (handoff-safe)."""

from typing import Any

import pytest

from agent_worker.coaching import CoachingListener
from vera_core.call_stream import TYPE_CALL_STATUS, TYPE_TRANSCRIPT, CallStreamEvent


class _FakeChatContext:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def copy(self) -> "_FakeChatContext":
        # Real ChatContext.copy() returns a new instance; messages already
        # added stay in the source, matching the shape _inject relies on.
        new = _FakeChatContext()
        new.messages = list(self.messages)
        return new

    def add_message(self, *, role: str, content: str) -> None:
        self.messages.append((role, content))


class _FakeAgent:
    def __init__(self) -> None:
        self.chat_ctx = _FakeChatContext()
        self.update_calls: list[_FakeChatContext] = []
        self.raise_on_update = False

    async def update_chat_ctx(self, ctx: _FakeChatContext) -> None:
        if self.raise_on_update:
            raise RuntimeError("boom")
        self.update_calls.append(ctx)
        self.chat_ctx = ctx


class _FakeSession:
    def __init__(self, agent: _FakeAgent) -> None:
        self.current_agent = agent


class _FakeStream:
    """Records the start_id it was asked to consume from and replays a fixed
    sequence of (entry_id, event)/None items."""

    def __init__(self, items: list[tuple[str, CallStreamEvent] | None]) -> None:
        self._items = items
        self.consumed_start_id: str | None = None
        self.consumed_room: str | None = None

    def consume(self, room_name: str, *, start_id: str = "0", **_kwargs: Any) -> Any:
        self.consumed_room = room_name
        self.consumed_start_id = start_id
        return self._aiter()

    async def _aiter(self) -> Any:
        for item in self._items:
            yield item


def _coaching_event(text: str, role: str = "coaching") -> CallStreamEvent:
    return CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": role, "source": "supervisor", "text": text}, ts=1
    )


@pytest.mark.asyncio
async def test_tails_from_now_not_from_the_start() -> None:
    """start_id="$" — a listener that (re)starts mid-call must never replay
    history and re-inject a stale note."""
    agent = _FakeAgent()
    stream = _FakeStream([])

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()

    assert stream.consumed_room == "room-1"
    assert stream.consumed_start_id == "$"


@pytest.mark.asyncio
async def test_coaching_turn_injected_as_a_system_message_never_a_reply() -> None:
    agent = _FakeAgent()
    stream = _FakeStream([("1-0", _coaching_event("ask about the deductible"))])

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()

    assert len(agent.update_calls) == 1
    role, content = agent.update_calls[0].messages[-1]
    assert role == "system"
    assert "ask about the deductible" in content
    # Never a bare copy of the customer's words — the note is clearly marked
    # as out-of-band so the LLM never mistakes it for something said on the call.
    assert "coaching note" in content.lower()


@pytest.mark.asyncio
async def test_whisper_turn_also_injected() -> None:
    agent = _FakeAgent()
    stream = _FakeStream([("1-0", _coaching_event("mention the copay", role="whisper"))])

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()

    assert len(agent.update_calls) == 1
    assert "mention the copay" in agent.update_calls[0].messages[-1][1]


@pytest.mark.asyncio
async def test_non_coaching_turns_and_other_event_types_are_ignored() -> None:
    agent = _FakeAgent()
    stream = _FakeStream(
        [
            None,  # idle keepalive tick
            ("1-0", CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "active"}, ts=1)),
            ("2-0", _coaching_event("", role="coaching")),  # empty text, skipped
            (
                "3-0",
                CallStreamEvent(
                    type=TYPE_TRANSCRIPT,
                    data={"role": "agent", "source": "bot", "text": "hi there"},
                    ts=1,
                ),
            ),
        ]
    )

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()

    assert agent.update_calls == []


@pytest.mark.asyncio
async def test_injection_targets_current_agent_at_injection_time_handoff_safe() -> None:
    """A handoff mid-run must land the note on whichever agent is live NOW —
    not a reference captured when run() started."""
    first_agent = _FakeAgent()
    second_agent = _FakeAgent()
    session = _FakeSession(first_agent)

    class _HandoffStream(_FakeStream):
        def consume(self, room_name: str, *, start_id: str = "0", **_kwargs: Any) -> Any:
            session.current_agent = second_agent  # handoff happens after run() starts
            return super().consume(room_name, start_id=start_id)

    stream = _HandoffStream([("1-0", _coaching_event("stay on script"))])

    await CoachingListener(session, stream, "room-1").run()

    assert first_agent.update_calls == []
    assert len(second_agent.update_calls) == 1


@pytest.mark.asyncio
async def test_injection_failure_is_logged_and_does_not_raise() -> None:
    agent = _FakeAgent()
    agent.raise_on_update = True
    stream = _FakeStream([("1-0", _coaching_event("ask about the deductible"))])

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()  # must not raise
