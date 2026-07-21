import asyncio

import pytest

from agent_worker.transcript_publisher import ReorderingEmitter, attach_transcript_publisher
from vera_core.transcript import (
    ROLE_AGENT,
    ROLE_DTMF,
    ROLE_USER,
    SOURCE_BOT,
    SOURCE_REP,
    TranscriptEvent,
    TurnRole,
    TurnSource,
)


class _RecordingSink:
    """Collects the ordered turns the emitter publishes. The emitter's contract is the
    `TurnPublisher` protocol, so these tests need no real stream service — only the
    order/attribution of what reaches a sink."""

    def __init__(self) -> None:
        self.turns: list[TranscriptEvent] = []

    async def publish_turn(
        self,
        room_name: str,
        role: TurnRole,
        text: str,
        *,
        ts: int,
        source: TurnSource | None = None,
    ) -> None:
        self.turns.append(
            TranscriptEvent.model_validate({"role": role, "source": source, "text": text, "ts": ts})
        )


class _UserEvent:
    # created_at mirrors the LiveKit field (time.time() seconds) — when the caller's final
    # transcript was produced.
    def __init__(self, transcript: str, is_final: bool, created_at: float = 0.0) -> None:
        self.transcript = transcript
        self.is_final = is_final
        self.created_at = created_at


class _Item:
    # created_at is the assistant turn's reply-start time (set before it speaks), so it stays
    # earlier than an interrupting caller turn even though the item is emitted late.
    def __init__(self, role: str, text: str, created_at: float = 0.0) -> None:
        self.role = role
        self.text_content = text
        self.created_at = created_at


class _ItemEvent:
    def __init__(self, item: _Item) -> None:
        self.item = item


class _AgentStateEvent:
    def __init__(self, new_state: str) -> None:
        self.new_state = new_state


def _service() -> _RecordingSink:
    return _RecordingSink()


async def _drain(
    emitter: ReorderingEmitter, svc: _RecordingSink, _room: str
) -> list[TranscriptEvent]:
    # aclose flushes any held turns and drains the ordered queue to the sink.
    await emitter.aclose()
    return svc.turns


def _rt(events: list[TranscriptEvent]) -> list[tuple[str, str]]:
    return [(e.role, e.text) for e in events]


def _state(em: ReorderingEmitter, state: str) -> None:
    em.on_agent_state(_AgentStateEvent(state))


@pytest.mark.asyncio
async def test_passthrough_without_interruption() -> None:
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    _state(em, "speaking")
    em.on_agent_item(_ItemEvent(_Item("assistant", "hi there", 1.0)))
    _state(em, "listening")
    em.on_user(_UserEvent("hello back", is_final=True, created_at=2.0))
    _state(em, "thinking")
    em.on_agent_item(_ItemEvent(_Item("assistant", "great", 3.0)))
    assert _rt(await _drain(em, svc, "room")) == [
        (ROLE_AGENT, "hi there"),
        (ROLE_USER, "hello back"),
        (ROLE_AGENT, "great"),
    ]


@pytest.mark.asyncio
async def test_barge_in_reorders_caller_behind_interrupted_agent() -> None:
    # The caller's final arrives while an agent turn is pending, BEFORE its item — but the
    # agent turn started first, so it must publish first.
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    _state(em, "speaking")  # agent begins the turn that will be interrupted
    em.on_user(_UserEvent("it's not covered", is_final=True, created_at=5.0))  # -> held
    em.on_agent_item(_ItemEvent(_Item("assistant", "is diagnostic", 3.0)))  # late commit
    assert _rt(await _drain(em, svc, "room")) == [
        (ROLE_AGENT, "is diagnostic"),
        (ROLE_USER, "it's not covered"),
    ]


@pytest.mark.asyncio
async def test_full_conversation_with_late_agent_item_orders_correctly() -> None:
    # Replays the reported failing run: the "Perfect, thanks..." agent turn finishes speaking
    # (agent_state -> listening) BEFORE its item commits, and the caller answers in that gap.
    # The caller turn must still land behind the agent turn that preceded it.
    svc = _service()
    em = ReorderingEmitter(svc, "room")

    _state(em, "speaking")  # greeting
    em.on_agent_item(_ItemEvent(_Item("assistant", "Hi, do you have a few minutes?", 1.0)))
    _state(em, "listening")

    em.on_user(_UserEvent("Yeah. Definitely.", is_final=True, created_at=2.0))  # agent idle

    _state(em, "thinking")  # agent begins "Perfect, thanks..."
    _state(em, "speaking")
    _state(em, "listening")  # audio done — but the item has NOT committed yet
    em.on_user(_UserEvent("No. It is not covered.", is_final=True, created_at=4.0))  # -> held
    em.on_agent_item(_ItemEvent(_Item("assistant", "Perfect, thanks. I need to verify", 3.0)))

    assert _rt(await _drain(em, svc, "room")) == [
        (ROLE_AGENT, "Hi, do you have a few minutes?"),
        (ROLE_USER, "Yeah. Definitely."),
        (ROLE_AGENT, "Perfect, thanks. I need to verify"),
        (ROLE_USER, "No. It is not covered."),
    ]


@pytest.mark.asyncio
async def test_caller_published_immediately_when_no_agent_turn_pending() -> None:
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    em.on_user(_UserEvent("hello", is_final=True, created_at=1.0))  # nothing pending -> immediate
    assert _rt(await _drain(em, svc, "room")) == [(ROLE_USER, "hello")]


@pytest.mark.asyncio
async def test_timeout_flushes_turn_held_for_an_agent_that_never_commits() -> None:
    svc = _service()
    em = ReorderingEmitter(svc, "room", hold_timeout=0.02)
    _state(em, "speaking")
    em.on_user(_UserEvent("hello", is_final=True, created_at=5.0))  # held (agent turn pending)
    await asyncio.sleep(0.05)  # exceed the safety-net timeout
    assert _rt(await _drain(em, svc, "room")) == [(ROLE_USER, "hello")]


@pytest.mark.asyncio
async def test_aclose_flushes_held_turns() -> None:
    svc = _service()
    em = ReorderingEmitter(svc, "room", hold_timeout=100.0)  # timeout won't fire
    _state(em, "speaking")
    em.on_user(_UserEvent("hello", is_final=True, created_at=5.0))  # held
    assert _rt(await _drain(em, svc, "room")) == [(ROLE_USER, "hello")]


@pytest.mark.asyncio
async def test_skips_interim_empty_and_non_assistant_turns() -> None:
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    em.on_user(_UserEvent("partial", is_final=False, created_at=1.0))
    em.on_user(_UserEvent("   ", is_final=True, created_at=2.0))
    em.on_agent_item(_ItemEvent(_Item("user", "echo", 3.0)))
    em.on_agent_item(_ItemEvent(_Item("assistant", "", 4.0)))
    assert await _drain(em, svc, "room") == []


@pytest.mark.asyncio
async def test_ts_derives_from_event_created_at() -> None:
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    em.on_agent_item(_ItemEvent(_Item("assistant", "hi", 1.5)))
    em.on_user(_UserEvent("yo", is_final=True, created_at=2.25))
    events = await _drain(em, svc, "room")
    assert [(e.role, e.ts) for e in events] == [(ROLE_AGENT, 1500), (ROLE_USER, 2250)]


@pytest.mark.asyncio
async def test_published_ts_never_goes_backwards() -> None:
    # A small created_at skew must not produce an out-of-order ts once emit order is decided.
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    em.on_agent_item(_ItemEvent(_Item("assistant", "first", 5.0)))
    em.on_user(_UserEvent("second", is_final=True, created_at=4.0))  # earlier ts, later emit
    events = await _drain(em, svc, "room")
    assert _rt(events) == [(ROLE_AGENT, "first"), (ROLE_USER, "second")]
    assert [e.ts for e in events] == [5000, 5000]  # clamped, not 5000 then 4000


@pytest.mark.asyncio
async def test_turns_carry_the_acting_source() -> None:
    # `source` is the actor (drives UI attribution); the emitter stamps it per turn so
    # consumers never derive it from the open-ended role vocabulary.
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    _state(em, "speaking")
    em.on_agent_item(_ItemEvent(_Item("assistant", "hi", 1.0)))
    em.on_user(_UserEvent("hello", is_final=True, created_at=2.0))
    events = await _drain(em, svc, "room")
    assert [(e.role, e.source) for e in events] == [
        (ROLE_AGENT, SOURCE_BOT),
        (ROLE_USER, SOURCE_REP),
    ]


@pytest.mark.asyncio
async def test_keypress_publishes_a_bot_dtmf_turn() -> None:
    # A successful press_keypad must leave evidence in the transcript: a dtmf-role turn
    # attributed to the bot, whose text is the digits sent.
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    em.on_user(_UserEvent("press 3 for claims", is_final=True, created_at=1.0))
    em.on_keypress("3")
    events = await _drain(em, svc, "room")
    assert [(e.role, e.source, e.text) for e in events] == [
        (ROLE_USER, SOURCE_REP, "press 3 for claims"),
        (ROLE_DTMF, SOURCE_BOT, "3"),
    ]


@pytest.mark.asyncio
async def test_keypress_is_held_behind_a_pending_agent_turn() -> None:
    # The agent may speak ("I'll select claims") and press in the same turn; its spoken
    # item commits late, so the keypress must wait behind it like a caller turn does.
    svc = _service()
    em = ReorderingEmitter(svc, "room")
    _state(em, "speaking")
    em.on_keypress("3")  # the speaking turn's item has not committed yet -> held
    em.on_agent_item(_ItemEvent(_Item("assistant", "I'll select claims", 1.0)))
    events = await _drain(em, svc, "room")
    assert [(e.role, e.text) for e in events] == [
        (ROLE_AGENT, "I'll select claims"),
        (ROLE_DTMF, "3"),
    ]


@pytest.mark.asyncio
async def test_attach_registers_all_handlers() -> None:
    registered: list[str] = []

    class _FakeSession:
        def on(self, event: str, cb: object) -> None:
            registered.append(event)

    em = attach_transcript_publisher(_FakeSession(), _service(), "room")
    assert set(registered) == {
        "user_input_transcribed",
        "conversation_item_added",
        "agent_state_changed",
    }
    await em.aclose()
