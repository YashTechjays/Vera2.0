"""Silence the agent for the rest of the call once a supervisor takes over.

The takeover is one-way by design: the call continues human-to-human, so the agent is
never un-muted (leaving it un-mutable would revive the bot if the supervisor's tab drops).
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from vera_core.observability.correlation import PARTICIPANT_MODE_INTERVENER

logger = logging.getLogger("agent_worker")


@dataclass(slots=True)
class TakeoverState:
    """One-way takeover latch, never reset. Lives in AgentSession.userdata: the only object
    both the plan runtime (built before the session) and the takeover controller (created
    after it starts) can reach."""

    engaged: bool = False


def intervener_present(mode_attrs: Iterable[str | None]) -> bool:
    return any(mode == PARTICIPANT_MODE_INTERVENER for mode in mode_attrs)


class _TakeoverSession(Protocol):
    @property
    def userdata(self) -> TakeoverState: ...


def takeover_engaged(session: _TakeoverSession) -> bool:
    # Protocol-typed so the bool stays a bool for mypy (Agent.session is AgentSession[Any]).
    return session.userdata.engaged


class _AudioToggle(Protocol):
    def set_audio_enabled(self, enable: bool) -> None: ...


class _SilenceableSession(_TakeoverSession, Protocol):
    def interrupt(self, *, force: bool = ...) -> object: ...

    @property
    def input(self) -> _AudioToggle: ...

    @property
    def output(self) -> _AudioToggle: ...


class AgentTakeoverController:
    """Silence the agent permanently the first time a supervisor intervenes.

    Idempotent: repeated room events after the first takeover are no-ops.
    """

    def __init__(
        self, session: _SilenceableSession, *, on_engage: Callable[[], None] | None = None
    ) -> None:
        self._session = session
        self._on_engage = on_engage

    @property
    def engaged(self) -> bool:
        return takeover_engaged(self._session)

    def engage(self) -> None:
        """Silence the agent, once. Safe to call on every room event."""
        if self.engaged:
            return
        # Set before interrupting: a tool call already in flight must see it as true.
        self._session.userdata.engaged = True
        self._session.interrupt(force=True)
        self._session.input.set_audio_enabled(False)
        self._session.output.set_audio_enabled(False)
        logger.info("agent silenced for supervisor takeover (permanent)")
        if self._on_engage is not None:
            self._on_engage()
