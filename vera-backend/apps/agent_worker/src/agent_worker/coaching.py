"""Coaching mode — the agent-worker half.

A supervisor's coaching/whisper message reaches this worker over the same
call-event stream the control plane's `/calls/{id}/coach` endpoint publishes
onto (see `vera_core.call_stream`). `CoachingListener` tails it for NEW
coaching/whisper turns only and folds each into Vera's chat context as a
system-role note — picked up naturally on her *next* turn, never forcing an
immediate reply and never interrupting the live conversation. See
`AgentSession.current_agent` / `Agent.update_chat_ctx` (livekit-agents): a
non-realtime `update_chat_ctx` call only replaces the agent's stored context
for the next turn generation to read; it does not cancel or affect a
turn already in flight.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.transcript import ROLE_COACHING, ROLE_WHISPER

logger = logging.getLogger("agent_worker")

# Cap so a long call doesn't accumulate unbounded notes resent every turn.
_MAX_COACHING_NOTES = 10

# A directive, not a suggestion: the model must not weigh this against its own
# sense of conversational flow and quietly skip it because the topic already
# passed - it must act on it next turn regardless. The customer must never
# learn a note was received, and Vera must never mistake it for something the
# caller/rep said - hence a system-role message (folded into the LLM's system
# instructions, not a conversational turn) with an explicit "don't mention
# this" instruction.
_COACHING_NOTE_PREFIX = (
    "[MANDATORY supervisor instruction - the customer did not say this. You "
    "MUST act on this on your very next turn, even if it means returning to "
    "a topic already covered - this takes priority over your own judgment "
    "about conversation flow. Phrase it naturally so the customer never "
    "realizes an instruction was received.] "
)


class _CoachableSession(Protocol):
    """Structural view of AgentSession — decouples this module (and its tests)
    from the concrete class and its userdata type parameter."""

    @property
    def current_agent(self) -> Any: ...


class _CoachingStream(Protocol):
    """Structural view of CallStreamService — just the read side this module
    needs, so tests can fake it without a real Redis-backed store."""

    def consume(
        self,
        room_name: str,
        *,
        start_id: str = "0",
        first_entry_deadline_s: float | None = None,
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]: ...


class CoachingListener:
    """Tails `stream` for coaching/whisper turns on `room_name`, starting from
    NOW (`start_id="$"` — never replays history, so a listener that restarts
    mid-call, e.g. after a redispatch, can't re-inject an old note).

    Targets `session.current_agent` at injection time (not a captured `Agent`
    reference), so a note lands on whichever agent is actually live even if a
    handoff (IVR navigator -> plan task agent -> next task agent) happened
    since `run()` started.
    """

    def __init__(self, session: _CoachableSession, stream: _CoachingStream, room_name: str) -> None:
        self._session = session
        self._stream = stream
        self._room_name = room_name

    async def run(self) -> None:
        async for entry in self._stream.consume(self._room_name, start_id="$"):
            if entry is None:
                continue  # idle-window keepalive tick — nothing to do
            try:
                await self._handle(entry)
            except asyncio.CancelledError:
                raise  # shutdown must still stop the loop
            except Exception as exc:
                # A stray failure (a bad note, a mid-handoff current_agent, a
                # Redis hiccup surfaced through the entry) must not silently
                # end coaching for the rest of the call — type name only,
                # never the note text (PHI).
                logger.warning("coaching listener hit %s; continuing", type(exc).__name__)

    async def _handle(self, entry: tuple[str, CallStreamEvent]) -> None:
        _entry_id, event = entry
        if event.type != TYPE_TRANSCRIPT:
            return
        if event.data.get("role") not in (ROLE_COACHING, ROLE_WHISPER):
            return
        text = str(event.data.get("text", "")).strip()
        if not text:
            return
        await self._inject(text)

    async def _inject(self, text: str) -> None:
        agent = self._session.current_agent
        ctx = agent.chat_ctx.copy()
        ctx.add_message(role="system", content=_COACHING_NOTE_PREFIX + text)

        notes = [item for item in ctx.items if _is_coaching_note(item)]
        if len(notes) > _MAX_COACHING_NOTES:
            evict_ids = {item.id for item in notes[: len(notes) - _MAX_COACHING_NOTES]}
            ctx.items = [item for item in ctx.items if item.id not in evict_ids]

        await agent.update_chat_ctx(ctx)


def _is_coaching_note(item: Any) -> bool:
    # Duck-typed, not isinstance - stays decoupled from the concrete livekit type.
    if getattr(item, "role", None) != "system":
        return False
    text = getattr(item, "text_content", None)
    return isinstance(text, str) and text.startswith(_COACHING_NOTE_PREFIX)
