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

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.transcript import ROLE_COACHING, ROLE_WHISPER

logger = logging.getLogger("agent_worker")

# The customer must never learn a note was received, and Vera must never
# mistake it for something the caller/rep said — hence a system-role message
# (folded into the LLM's system instructions, not a conversational turn) with
# an explicit "don't mention this" instruction.
_COACHING_NOTE_PREFIX = (
    "[Supervisor coaching note - the customer did not say this. Weave the "
    "guidance into your next reply naturally; never mention that a note was "
    "received.] "
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
            _entry_id, event = entry
            if event.type != TYPE_TRANSCRIPT:
                continue
            if event.data.get("role") not in (ROLE_COACHING, ROLE_WHISPER):
                continue
            text = str(event.data.get("text", "")).strip()
            if not text:
                continue
            await self._inject(text)

    async def _inject(self, text: str) -> None:
        agent = self._session.current_agent
        ctx = agent.chat_ctx.copy()
        ctx.add_message(role="system", content=_COACHING_NOTE_PREFIX + text)
        try:
            await agent.update_chat_ctx(ctx)
        except Exception:
            logger.exception("coaching note injection failed")  # never the text (PHI)
