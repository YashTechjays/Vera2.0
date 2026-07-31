"""Silence the agent for the rest of the call once a supervisor takes over.

The takeover is one-way by design: the call continues human-to-human, so the agent is
never un-muted (leaving it un-mutable would revive the bot if the supervisor's tab drops).

Also holds the pending-coaching-notes queue, which has the same reachability problem as
takeover and so lives on the same userdata object.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from vera_core.observability.correlation import PARTICIPANT_MODE_INTERVENER

logger = logging.getLogger("agent_worker")

# Cap so a burst of notes arriving between two turns can't grow unbounded.
_MAX_COACHING_NOTES = 10


@dataclass(slots=True)
class TakeoverState:
    """One-way takeover latch (never reset) plus the coaching-notes queue. Lives in
    AgentSession.userdata — the only object both the plan runtime (built before the session)
    and the takeover controller / CoachingListener (created after) can reach."""

    engaged: bool = False
    pending_coaching_notes: list[str] = field(default_factory=list)


def intervener_present(mode_attrs: Iterable[str | None]) -> bool:
    return any(mode == PARTICIPANT_MODE_INTERVENER for mode in mode_attrs)


class _TakeoverSession(Protocol):
    @property
    def userdata(self) -> TakeoverState: ...


def takeover_engaged(session: _TakeoverSession) -> bool:
    # Protocol-typed so the bool stays a bool for mypy (Agent.session is AgentSession[Any]).
    return session.userdata.engaged


def push_coaching_note(session: _TakeoverSession, note: str) -> None:
    """Queue a coaching note for the next turn, evicting the oldest past the cap."""
    notes = session.userdata.pending_coaching_notes
    notes.append(note)
    excess = len(notes) - _MAX_COACHING_NOTES
    if excess > 0:
        del notes[:excess]


def take_pending_coaching_notes(session: _TakeoverSession) -> list[str]:
    """Drain the queue — a note is one-shot; left queued it re-issues every turn and Vera
    never stops re-asking (VR2-97)."""
    notes = session.userdata.pending_coaching_notes
    drained = notes.copy()
    notes.clear()
    return drained


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
