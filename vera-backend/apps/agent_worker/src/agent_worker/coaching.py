"""Coaching mode — the agent-worker half.

A supervisor's coaching/whisper message arrives over the call-event stream the control
plane's `/calls/{id}/coach` endpoint publishes onto (`vera_core.call_stream`).
`CoachingListener` tails it and queues each note; `apply_pending_coaching_notes` drains
the queue onto the turn context from an `Agent.on_user_turn_completed` override
(`plan_runtime.py`).

Two constraints that read like accidents and are not (VR2-97):
* Apply from that hook, never a background task — it runs on the same call stack livekit
  uses to validate a preemptively generated reply (`is_equivalent`), so a stale
  speculative reply is discarded and regenerated with the note in it.
* Deliver each note ONCE — its text overrides the plan's "don't re-ask" ground rule, so a
  note left queued re-issues that override every turn and Vera never stops re-asking. A
  note consumed on a turn livekit then abandons is lost; the supervisor resends.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Literal, Protocol

from agent_worker.intervention import (
    TakeoverState,
    push_coaching_note,
    take_pending_coaching_notes,
)
from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.transcript import ROLE_COACHING, ROLE_WHISPER

logger = logging.getLogger("agent_worker")

# A vague "overrides your ground rules" claim lost silently to the specific "do not
# repeat" rule (VR2-97) until named explicitly - but coaching text is free-form and
# unpredictable, so a fixed list of named exceptions never covers everything a
# supervisor might ask for. Framed as authority (a human watching the live call has
# context you don't) with named examples as anchors, not the full list - an explicit
# catch-all covers whatever we didn't think to name.
_COACHING_NOTE_PREFIX = (
    "[MANDATORY supervisor coaching - a human supervisor is listening to this call "
    "live and has context you do not. The customer did not say this. Treat it like a "
    "coach correcting you mid-performance: do exactly what they say on your very next "
    "turn, no matter what it is, even if it conflicts with your standing ground rules "
    "and your own judgment about conversation flow - for example, repeating a question "
    "that has already been answered, asking more than one question at a time, "
    "volunteering information the representative did not ask for, or pressing again "
    "after they could not answer, or anything else that conflicts with your normal "
    "rules. Their judgment about this exact moment overrides yours. Phrase it "
    "naturally so the customer never realizes an instruction was received.] "
)


class _CoachableSession(Protocol):
    """Structural view of AgentSession, matching `intervention._TakeoverSession`."""

    @property
    def userdata(self) -> TakeoverState: ...


class _CoachingStream(Protocol):
    """Structural view of CallStreamService — the read side only."""

    def consume(
        self,
        room_name: str,
        *,
        start_id: str = "0",
        first_entry_deadline_s: float | None = None,
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]: ...


class _MutableChatCtx(Protocol):
    """Structural view of the turn_ctx handed to `Agent.on_user_turn_completed` (`role` is
    narrowed because the real `add_message` takes a ChatRole literal, not `str`)."""

    def add_message(self, *, role: Literal["system"], content: str) -> object: ...


class CoachingListener:
    """Queues coaching/whisper notes for `room_name`, tailing from NOW (`start_id="$"`) so a
    listener restarted mid-call — after a redispatch — can't replay a stale note."""

    def __init__(self, session: _CoachableSession, stream: _CoachingStream, room_name: str) -> None:
        self._session = session
        self._stream = stream
        self._room_name = room_name

    async def run(self) -> None:
        async for entry in self._stream.consume(self._room_name, start_id="$"):
            if entry is None:
                continue  # idle keepalive tick
            try:
                self._handle(entry)
            except asyncio.CancelledError:
                raise  # shutdown must still stop the loop
            except Exception as exc:
                # One bad entry must not end coaching for the call; type name only, never
                # the note text (PHI).
                logger.warning("coaching listener hit %s; continuing", type(exc).__name__)

    def _handle(self, entry: tuple[str, CallStreamEvent]) -> None:
        _entry_id, event = entry
        if event.type != TYPE_TRANSCRIPT:
            return
        if event.data.get("role") not in (ROLE_COACHING, ROLE_WHISPER):
            return
        text = str(event.data.get("text", "")).strip()
        if not text:
            return
        push_coaching_note(self._session, _COACHING_NOTE_PREFIX + text)


def apply_pending_coaching_notes(session: _CoachableSession, turn_ctx: _MutableChatCtx) -> None:
    """Move each queued note onto the turn about to be generated (see module docstring)."""
    for note in take_pending_coaching_notes(session):
        turn_ctx.add_message(role="system", content=note)
