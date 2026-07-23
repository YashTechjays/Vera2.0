"""CoachingListener: tails the call-event stream for coaching/whisper turns and
folds each into the currently active agent's chat context — never forcing an
immediate reply, always targeting session.current_agent (handoff-safe)."""

from typing import Any

import pytest

from agent_worker.coaching import _MAX_COACHING_NOTES, CoachingListener
from vera_core.call_stream import TYPE_CALL_STATUS, TYPE_TRANSCRIPT, CallStreamEvent


class _FakeChatItem:
    """Mimics the sliver of the real ChatMessage shape _inject relies on
    (.id/.role/.text_content) so the eviction logic can run against it."""

    _next_id = 0

    def __init__(self, role: str, content: str) -> None:
        _FakeChatItem._next_id += 1
        self.id = f"item-{_FakeChatItem._next_id}"
        self.role = role
        self.text_content = content


class _FakeChatContext:
    def __init__(self) -> None:
        self.items: list[_FakeChatItem] = []

    def copy(self) -> "_FakeChatContext":
        # Real ChatContext.copy() returns a new instance; messages already
        # added stay in the source, matching the shape _inject relies on.
        new = _FakeChatContext()
        new.items = list(self.items)
        return new

    def add_message(self, *, role: str, content: str) -> None:
        self.items.append(_FakeChatItem(role, content))

    @property
    def messages(self) -> list[tuple[str, str]]:
        return [(item.role, item.text_content) for item in self.items]


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
    assert "supervisor instruction" in content.lower()
    # Framed as mandatory, not a suggestion the model can weigh against its own
    # sense of conversation flow and quietly skip.
    assert "must" in content.lower()


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
async def test_coaching_notes_are_capped_evicting_the_oldest_first() -> None:
    """A long call must not accumulate unbounded notes in the chat context —
    once the cap is exceeded, the oldest note is dropped, not the newest."""
    agent = _FakeAgent()
    total = _MAX_COACHING_NOTES + 3
    stream = _FakeStream([(f"{i}-0", _coaching_event(f"note {i}")) for i in range(1, total + 1)])

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()

    final_ctx = agent.update_calls[-1]
    assert len(final_ctx.items) == _MAX_COACHING_NOTES
    contents = [content for _, content in final_ctx.messages]
    # endswith, not `in` - "note 1" is a substring of "note 11", "note 12"...
    assert not any(c.endswith(f"note {i}") for i in range(1, 4) for c in contents)
    assert any(c.endswith(f"note {total}") for c in contents)  # newest kept


@pytest.mark.asyncio
async def test_injection_failure_is_logged_and_does_not_raise() -> None:
    agent = _FakeAgent()
    agent.raise_on_update = True
    stream = _FakeStream([("1-0", _coaching_event("ask about the deductible"))])

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()  # must not raise


@pytest.mark.asyncio
async def test_a_failed_note_does_not_end_coaching_for_the_rest_of_the_call() -> None:
    """One bad entry (injection failure, or any other stray exception) must
    not kill the listener task - later notes on the same call still land."""
    agent = _FakeAgent()
    agent.raise_on_update = True
    stream = _FakeStream(
        [
            ("1-0", _coaching_event("this one fails")),
            ("2-0", _coaching_event("this one must still land")),
        ]
    )

    original_consume = stream.consume

    async def _consume_then_recover(room_name: str, *, start_id: str = "0", **_kwargs: Any) -> Any:
        async for item in original_consume(room_name, start_id=start_id):
            if item is not None and item[0] == "2-0":
                agent.raise_on_update = False  # second note should succeed
            yield item

    stream.consume = _consume_then_recover  # type: ignore[method-assign]

    await CoachingListener(_FakeSession(agent), stream, "room-1").run()

    assert len(agent.update_calls) == 1
    assert "this one must still land" in agent.update_calls[0].messages[-1][1]
