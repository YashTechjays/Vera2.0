"""Silence the agent for the rest of the call once a supervisor takes over.

The takeover is one-way by design: the call continues human-to-human, so the agent is
never un-muted (leaving it un-mutable would revive the bot if the supervisor's tab drops).
"""

import logging
from collections.abc import Iterable
from typing import Protocol

from vera_core.observability.correlation import PARTICIPANT_MODE_INTERVENER

logger = logging.getLogger("agent_worker")


def intervener_present(mode_attrs: Iterable[str | None]) -> bool:
    return any(mode == PARTICIPANT_MODE_INTERVENER for mode in mode_attrs)


class _AudioToggle(Protocol):
    def set_audio_enabled(self, enable: bool) -> None: ...


class _SilenceableSession(Protocol):
    def interrupt(self, *, force: bool = ...) -> object: ...

    @property
    def input(self) -> _AudioToggle: ...

    @property
    def output(self) -> _AudioToggle: ...


class AgentTakeoverController:
    """Silence the agent permanently the first time a supervisor intervenes.

    Idempotent: repeated room events after the first takeover are no-ops.
    """

    def __init__(self, session: _SilenceableSession) -> None:
        self._session = session
        self._engaged = False

    @property
    def engaged(self) -> bool:
        return self._engaged

    def engage(self) -> None:
        """Silence the agent, once. Safe to call on every room event."""
        if self._engaged:
            return
        self._engaged = True
        self._session.interrupt(force=True)
        self._session.input.set_audio_enabled(False)
        self._session.output.set_audio_enabled(False)
        logger.info("agent silenced for supervisor takeover (permanent)")
