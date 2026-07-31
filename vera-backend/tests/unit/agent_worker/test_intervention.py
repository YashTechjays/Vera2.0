"""A supervisor takeover silences the agent permanently — it never resumes."""

from agent_worker.intervention import (
    _MAX_COACHING_NOTES,
    AgentTakeoverController,
    TakeoverState,
    intervener_present,
    push_coaching_note,
    take_pending_coaching_notes,
    takeover_engaged,
)


class _FakeAudio:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_audio_enabled(self, enable: bool) -> None:
        self.calls.append(enable)


class _FakeSession:
    def __init__(self) -> None:
        self.interrupts = 0
        self.input = _FakeAudio()
        self.output = _FakeAudio()
        self.userdata = TakeoverState()

    def interrupt(self, *, force: bool = False) -> object:
        assert force is True
        self.interrupts += 1
        return None


def test_intervener_present_detection() -> None:
    assert intervener_present([]) is False
    assert intervener_present(["listener", None, "listener"]) is False
    assert intervener_present(["listener", "intervener"]) is True
    assert intervener_present(["intervener"]) is True


def test_engage_silences_the_agent_once() -> None:
    session = _FakeSession()
    ctl = AgentTakeoverController(session)

    ctl.engage()
    assert ctl.engaged is True
    assert session.interrupts == 1
    assert session.input.calls == [False]
    assert session.output.calls == [False]


def test_engage_publishes_the_flag_agents_read() -> None:
    """The controller and the agents' guards must never disagree: one latch, one truth."""
    session = _FakeSession()
    ctl = AgentTakeoverController(session)
    assert takeover_engaged(session) is False

    ctl.engage()
    assert takeover_engaged(session) is True
    assert ctl.engaged is takeover_engaged(session)


def test_engage_is_idempotent_and_never_resumes() -> None:
    session = _FakeSession()
    ctl = AgentTakeoverController(session)

    ctl.engage()
    # Repeated room events must never re-interrupt or re-enable the agent's audio.
    ctl.engage()
    ctl.engage()
    assert session.interrupts == 1
    assert session.input.calls == [False]  # never toggled back to True
    assert session.output.calls == [False]


def test_untouched_until_a_supervisor_takes_over() -> None:
    session = _FakeSession()
    ctl = AgentTakeoverController(session)
    assert ctl.engaged is False
    assert session.interrupts == 0
    assert session.input.calls == []
    assert session.output.calls == []


def test_on_engage_fires_once() -> None:
    session = _FakeSession()
    calls: list[int] = []
    ctl = AgentTakeoverController(session, on_engage=lambda: calls.append(1))

    ctl.engage()
    ctl.engage()
    assert calls == [1]


def test_pushed_notes_are_taken_in_order() -> None:
    session = _FakeSession()
    push_coaching_note(session, "note one")
    push_coaching_note(session, "note two")

    assert take_pending_coaching_notes(session) == ["note one", "note two"]


def test_taking_notes_drains_the_queue() -> None:
    """A note is one-shot: taken once, then gone, so it can't re-issue itself (VR2-97)."""
    session = _FakeSession()
    push_coaching_note(session, "note one")

    assert take_pending_coaching_notes(session) == ["note one"]
    assert take_pending_coaching_notes(session) == []


def test_push_coaching_note_evicts_the_oldest_past_the_cap() -> None:
    """Past the cap the oldest note is dropped, not the newest."""
    session = _FakeSession()
    overflow = 2
    notes = [f"note {i}" for i in range(_MAX_COACHING_NOTES + overflow)]
    for note in notes:
        push_coaching_note(session, note)

    assert take_pending_coaching_notes(session) == notes[overflow:]
