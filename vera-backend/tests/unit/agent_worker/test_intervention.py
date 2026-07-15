"""The agent pauses on a supervisor takeover and resumes when it ends."""

from agent_worker.intervention import AgentPauseController, intervener_present


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

    def interrupt(self, *, force: bool = False) -> object:
        assert force is True
        self.interrupts += 1
        return None


def test_intervener_present_detection() -> None:
    assert intervener_present([]) is False
    assert intervener_present(["listener", None, "listener"]) is False
    assert intervener_present(["listener", "intervener"]) is True
    assert intervener_present(["intervener"]) is True


def test_pause_fires_once_on_rising_edge() -> None:
    session = _FakeSession()
    ctl = AgentPauseController(session)

    ctl.apply(True)
    assert ctl.paused is True
    assert session.interrupts == 1
    assert session.input.calls == [False]
    assert session.output.calls == [False]

    # Repeated "present" events (a listener joins, an attribute refresh) are no-ops.
    ctl.apply(True)
    assert session.interrupts == 1
    assert session.input.calls == [False]
    assert session.output.calls == [False]


def test_resume_fires_once_on_falling_edge() -> None:
    session = _FakeSession()
    ctl = AgentPauseController(session)
    ctl.apply(True)

    ctl.apply(False)
    assert ctl.paused is False
    assert session.input.calls == [False, True]
    assert session.output.calls == [False, True]

    # A second "absent" event does nothing more.
    ctl.apply(False)
    assert session.input.calls == [False, True]
    assert session.output.calls == [False, True]
    assert session.interrupts == 1  # never interrupts on resume


def test_no_action_when_never_intervened() -> None:
    session = _FakeSession()
    ctl = AgentPauseController(session)
    ctl.apply(False)
    assert ctl.paused is False
    assert session.interrupts == 0
    assert session.input.calls == []
    assert session.output.calls == []
