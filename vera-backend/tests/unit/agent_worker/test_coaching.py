"""CoachingListener queues coaching/whisper notes; `apply_pending_coaching_notes` drains
them onto one turn context (VR2-97 — see the module under test for why both halves)."""

from typing import Any

import pytest

from agent_worker.coaching import CoachingListener, apply_pending_coaching_notes
from agent_worker.intervention import _MAX_COACHING_NOTES, TakeoverState, push_coaching_note
from vera_core.call_stream import TYPE_CALL_STATUS, TYPE_TRANSCRIPT, CallStreamEvent


class _FakeSession:
    def __init__(self) -> None:
        self.userdata = TakeoverState()


def _queued(session: _FakeSession) -> list[str]:
    """Notes still waiting — read directly, so "queued" stays distinguishable from "delivered"."""
    return session.userdata.pending_coaching_notes


class _FakeStream:
    """Records the start_id it was asked to consume from and replays a fixed
    sequence of (entry_id, event)/None items."""

    def __init__(self, items: list[tuple[str, Any] | None]) -> None:
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


class _FakeTurnCtx:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def add_message(self, *, role: str, content: str) -> None:
        self.messages.append((role, content))


class _MalformedEvent:
    """A stray bad entry: `.data` isn't a dict, so `_handle`'s `.get(...)` raises."""

    type = TYPE_TRANSCRIPT
    data = None


def _coaching_event(text: str, role: str = "coaching") -> CallStreamEvent:
    return CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": role, "source": "supervisor", "text": text}, ts=1
    )


@pytest.mark.asyncio
async def test_tails_from_now_not_from_the_start() -> None:
    """A listener restarting mid-call must not replay history and re-push a stale note."""
    session = _FakeSession()
    stream = _FakeStream([])

    await CoachingListener(session, stream, "room-1").run()

    assert stream.consumed_room == "room-1"
    assert stream.consumed_start_id == "$"


@pytest.mark.asyncio
async def test_coaching_turn_pushed_into_the_pending_queue() -> None:
    session = _FakeSession()
    stream = _FakeStream([("1-0", _coaching_event("ask about the deductible"))])

    await CoachingListener(session, stream, "room-1").run()

    notes = _queued(session)
    assert len(notes) == 1
    assert "ask about the deductible" in notes[0]
    # Marked out-of-band so the LLM never mistakes it for something said on the call.
    assert "supervisor" in notes[0].lower()
    assert "the customer did not say this" in notes[0].lower()
    # Mandatory, not a suggestion the model can weigh against conversation flow and skip.
    assert "mandatory" in notes[0].lower()
    assert "no matter what" in notes[0].lower()


@pytest.mark.asyncio
async def test_coaching_note_explicitly_overrides_the_dont_repeat_ground_rule() -> None:
    """A coached repeat loses to the standing "don't re-ask" rule unless the note names it."""
    session = _FakeSession()
    stream = _FakeStream([("1-0", _coaching_event("repeat the previous question politely"))])

    await CoachingListener(session, stream, "room-1").run()

    content = _queued(session)[0].lower()
    assert "ground rule" in content
    assert "already been answered" in content


@pytest.mark.asyncio
async def test_coaching_note_names_other_ground_rules_it_may_need_to_override() -> None:
    """Same lesson as the repeat case, generalized: a vague "overrides your ground
    rules" claim wasn't enough on its own - only naming the specific rule made the
    override actually win. Coaching text is free-form and unpredictable, so the
    prefix should name every standing rule (vera_core.forms.prompting.
    FACTORY_SESSION.base_instructions) a supervisor override is plausibly meant to
    beat, not just "repeat" - not just the one instruction type we happened to test."""
    session = _FakeSession()
    stream = _FakeStream([("1-0", _coaching_event("go ahead and mention the deductible"))])

    await CoachingListener(session, stream, "room-1").run()

    content = _queued(session)[0].lower()
    assert "one question at a time" in content
    assert "volunteer" in content
    assert "pressing" in content


@pytest.mark.asyncio
async def test_coaching_note_frames_override_as_a_catch_all_not_a_fixed_list() -> None:
    """Named examples (repeat, multiple-questions, volunteer, pressing) anchor the
    model on what "override" means, but coaching text is unpredictable - a supervisor
    watching the live call can ask for anything. The prefix must say those examples
    aren't the full list, or a scenario we never thought to name silently loses to a
    standing rule the same way "repeat" once did."""
    session = _FakeSession()
    stream = _FakeStream([("1-0", _coaching_event("wrap up the call early, the rep is annoyed"))])

    await CoachingListener(session, stream, "room-1").run()

    content = _queued(session)[0].lower()
    assert "anything else" in content or "no matter what" in content


@pytest.mark.asyncio
async def test_whisper_turn_also_pushed() -> None:
    session = _FakeSession()
    stream = _FakeStream([("1-0", _coaching_event("mention the copay", role="whisper"))])

    await CoachingListener(session, stream, "room-1").run()

    assert "mention the copay" in _queued(session)[0]


@pytest.mark.asyncio
async def test_non_coaching_turns_and_other_event_types_are_ignored() -> None:
    session = _FakeSession()
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

    await CoachingListener(session, stream, "room-1").run()

    assert _queued(session) == []


@pytest.mark.asyncio
async def test_coaching_notes_are_capped_evicting_the_oldest_first() -> None:
    """Past the cap the oldest note is dropped, not the newest."""
    session = _FakeSession()
    total = _MAX_COACHING_NOTES + 3
    stream = _FakeStream([(f"{i}-0", _coaching_event(f"note {i}")) for i in range(1, total + 1)])

    await CoachingListener(session, stream, "room-1").run()

    notes = _queued(session)
    assert len(notes) == _MAX_COACHING_NOTES
    # endswith, not `in` - "note 1" is a substring of "note 11", "note 12"...
    assert not any(n.endswith(f"note {i}") for i in range(1, 4) for n in notes)
    assert any(n.endswith(f"note {total}") for n in notes)  # newest kept


@pytest.mark.asyncio
async def test_a_bad_entry_does_not_end_coaching_for_the_rest_of_the_call() -> None:
    """One bad entry must not kill the listener — later notes on the call still land."""
    session = _FakeSession()
    stream = _FakeStream(
        [
            ("1-0", _MalformedEvent()),
            ("2-0", _coaching_event("this one must still land")),
        ]
    )

    await CoachingListener(session, stream, "room-1").run()

    notes = _queued(session)
    assert len(notes) == 1
    assert "this one must still land" in notes[0]


def test_apply_pending_coaching_notes_folds_each_onto_turn_ctx() -> None:
    session = _FakeSession()
    push_coaching_note(session, "note one")
    push_coaching_note(session, "note two")
    turn_ctx = _FakeTurnCtx()

    apply_pending_coaching_notes(session, turn_ctx)

    assert turn_ctx.messages == [("system", "note one"), ("system", "note two")]


def test_apply_pending_coaching_notes_is_a_noop_with_nothing_pending() -> None:
    session = _FakeSession()
    turn_ctx = _FakeTurnCtx()

    apply_pending_coaching_notes(session, turn_ctx)

    assert turn_ctx.messages == []


def test_a_delivered_note_is_consumed_and_never_reissued() -> None:
    """Repeat-loop regression: a note left queued re-issued itself every turn forever."""
    session = _FakeSession()
    push_coaching_note(session, "note one")

    first_turn_ctx = _FakeTurnCtx()
    apply_pending_coaching_notes(session, first_turn_ctx)
    second_turn_ctx = _FakeTurnCtx()
    apply_pending_coaching_notes(session, second_turn_ctx)

    assert first_turn_ctx.messages == [("system", "note one")]
    assert second_turn_ctx.messages == []
    assert _queued(session) == []
