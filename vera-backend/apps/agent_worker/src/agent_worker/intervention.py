"""Silence the agent for the rest of the call once a supervisor takes over.

The agent's audio input is pinned to the SIP callee (see main.build_room_input_options),
so it never hears — and never yields to — a supervisor who intervenes. This watches the
room for a participant carrying the intervene mode attribute and, the first time one
appears, cuts the agent off and mutes it.

A takeover is **one-way**: from that point the call continues human-to-human (supervisor
↔ caller), so the agent never speaks again — it is deliberately NOT un-muted if the
supervisor later leaves, which would otherwise revive the bot mid-conversation (e.g. on a
dropped supervisor tab). Detection is pure and unit-tested; the session controls are the
framework's own interrupt / audio toggles.
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


class _SilenceableSession(Protocol):
    """The slice of AgentSession this controller drives."""

    def interrupt(self, *, force: bool = ...) -> object: ...

    @property
    def input(self) -> _AudioToggle: ...

    @property
    def output(self) -> _AudioToggle: ...


class AgentTakeoverController:
    """Silence the agent permanently the first time a supervisor intervenes.

    Idempotent — repeated room events after the first takeover are no-ops, so the
    agent is silenced (and interrupted) exactly once and never un-muted.
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
        self._session.interrupt(force=True)  # cut off any current utterance
        self._session.input.set_audio_enabled(False)  # stop hearing the call
        self._session.output.set_audio_enabled(False)  # stop speaking
        logger.info("agent silenced for supervisor takeover (permanent)")
