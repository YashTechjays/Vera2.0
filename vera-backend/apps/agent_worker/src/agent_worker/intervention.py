"""Silence the agent while a supervisor is intervening (a "takeover").

The agent's audio input is pinned to the SIP callee (see main.build_room_input_options),
so it never hears — and never yields to — a supervisor who intervenes. This watches the
room for a participant carrying the intervene mode attribute and, on the rising edge,
cuts the agent off and mutes it; on the falling edge it resumes. Detection is pure and
unit-tested; the session controls are the framework's own interrupt / audio toggles.
"""

import logging
from collections.abc import Iterable
from typing import Protocol

from vera_core.observability.correlation import PARTICIPANT_MODE_INTERVENER

logger = logging.getLogger("agent_worker")


def intervener_present(mode_attrs: Iterable[str | None]) -> bool:
    """True if any participant's vera.mode attribute marks them an intervener."""
    return any(mode == PARTICIPANT_MODE_INTERVENER for mode in mode_attrs)


class _AudioToggle(Protocol):
    def set_audio_enabled(self, enable: bool) -> None: ...


class _PausableSession(Protocol):
    """The slice of AgentSession this controller drives."""

    def interrupt(self, *, force: bool = ...) -> object: ...

    @property
    def input(self) -> _AudioToggle: ...

    @property
    def output(self) -> _AudioToggle: ...


class AgentPauseController:
    """Pause the agent while a supervisor intervenes, resume when they leave.

    Idempotent by design: pause/resume fire only on the transition, so repeated
    room events (a listener joining, an attribute refresh) never re-trigger them.
    """

    def __init__(self, session: _PausableSession) -> None:
        self._session = session
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def apply(self, intervener_active: bool) -> None:
        if intervener_active and not self._paused:
            self._paused = True
            self._session.interrupt(force=True)  # cut off any current utterance
            self._session.input.set_audio_enabled(False)  # stop hearing the call
            self._session.output.set_audio_enabled(False)  # stop speaking
            logger.info("agent paused for supervisor intervention")
        elif not intervener_active and self._paused:
            self._paused = False
            self._session.input.set_audio_enabled(True)
            self._session.output.set_audio_enabled(True)
            logger.info("agent resumed after supervisor intervention ended")
