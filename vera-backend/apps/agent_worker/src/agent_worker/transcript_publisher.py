"""Publish finalized, de-identified transcript turns via the TranscriptService — in true
chronological order, even when the caller barges in over the agent.

Taps AgentSession events on the de-identified side of the PHI wall: user turns are the
redacted FINAL transcript (post stt_node); agent turns are the LLM's token-only output
(pre tts_node hydration). Best-effort — a Redis failure logs and is swallowed, never
breaking the call.

## Why a reordering emitter (the barge-in problem)

LiveKit emits the interrupted agent's `conversation_item_added` only *after* the truncated
speech commits — which is *after* the caller's interrupting `user_input_transcribed` final.
So the raw arrival order is wrong: the caller's line lands ahead of the agent line it cut
off. The transport (a Redis Stream) is append-only, so once a turn is written its position
is frozen; ordering therefore has to be enforced *here*, at the producer, before the write —
that way every consumer (live SSE today, the DB finalizer later) reads an already-correct
stream and never re-implements a sort.

We fix it at the source by holding caller turns while an agent turn is *pending*:

* An agent turn is pending from the moment its audio starts (`agent_state` enters
  `speaking`) until its `conversation_item_added` actually commits. That commit lags well
  past the end of speech — observed ~1.8s after `agent_state` returns to `listening` — so a
  caller final that lands after the agent *stopped* speaking, but before its item arrives,
  would otherwise publish ahead of it. (We key off `speaking`, not `thinking`: audio only
  starts once the triggering caller turn has committed, so preemptive generation can't make
  us hold a caller turn behind the agent's own response to it.)
* So we hold a caller final whenever an agent turn is pending, and release it once that turn
  commits (its item is emitted first, the caller turn behind it). With nothing pending, a
  caller final publishes immediately. (Turns are sequential — each item commits before the
  next turn speaks — so at most one is pending; a counter suffices.)
* A timeout is a safety net: if a pending turn never commits, held turns flush anyway (and
  the counter resets) so the live transcript can't stall.

Turns are serialized onto the stream through a single ordered queue, and each turn's `ts`
is its LiveKit `created_at` (agent → the item's reply-start time), clamped to be
monotonically non-decreasing so the published order and the `ts` field never disagree.
"""

import asyncio
import logging
import time
from typing import Any, Literal

from vera_core.transcript import ROLE_AGENT, ROLE_USER, TranscriptService

logger = logging.getLogger("agent_worker")

# Safety net: if a held caller turn is never released (the interrupted agent turn never
# commits), flush it after this long so the live transcript never stalls.
_HOLD_TIMEOUT_S = 2.0

type _Role = Literal["user", "agent"]
type _Turn = tuple[float, _Role, str]  # (created_at seconds, role, text)


def _ts_s(created_at: Any) -> float:
    """Seconds from a LiveKit event's `created_at`, falling back to wall-clock now if it is
    missing or non-numeric (older event shapes / handoff items)."""
    if isinstance(created_at, int | float):
        return float(created_at)
    return time.time()


class ReorderingEmitter:
    """Serializes finalized turns onto the transcript stream in chronological order.

    Holds a caller turn while an agent turn is pending (started but its item not yet
    committed), then emits it behind that turn. See the module docstring. All state is touched
    only from synchronous session events on the single event loop, so no locking is needed;
    the ordered queue is drained by one worker task.
    """

    def __init__(
        self, service: TranscriptService, room_name: str, *, hold_timeout: float = _HOLD_TIMEOUT_S
    ) -> None:
        self._service = service
        self._room = room_name
        self._hold_timeout = hold_timeout
        self._loop = asyncio.get_running_loop()
        # agent turns whose audio has started (agent_state entered speaking) but whose item has
        # not yet committed; `_agent_speaking` tracks the state so we increment once per turn.
        self._pending = 0
        self._agent_speaking = False
        # Caller turns held while an agent turn is pending, kept ts-sorted; released when that
        # turn's item commits (or by the timeout).
        self._buffer: list[_Turn] = []
        self._queue: asyncio.Queue[_Turn | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] = self._loop.create_task(self._drain())
        self._timeout_handle: asyncio.TimerHandle | None = None
        self._last_ts = float("-inf")  # last emitted ts (s); enforces monotonic publish order

    # --- ingress: synchronous AgentSession event handlers ---

    def on_user(self, ev: Any) -> None:
        if not ev.is_final:
            return
        text = (ev.transcript or "").strip()
        if not text:
            return
        # created_at marks when the caller's final transcript was produced.
        ts = _ts_s(getattr(ev, "created_at", None))
        if self._pending > 0:
            logger.debug(
                "transcript: holding caller turn behind %d pending agent turn(s)", self._pending
            )
            self._hold((ts, ROLE_USER, text))
        else:
            self._emit(ts, ROLE_USER, text)

    def on_agent_item(self, ev: Any) -> None:
        item = ev.item
        if getattr(item, "role", None) != "assistant":
            return  # user echo / tool-call items; the caller side comes via on_user
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            return
        # This agent turn committed; emit it, then release the caller turns it was blocking
        # (once nothing else is pending, so all earlier agent turns are out first).
        self._pending = max(0, self._pending - 1)
        self._emit(_ts_s(getattr(item, "created_at", None)), ROLE_AGENT, text)
        if self._pending == 0:
            self._release_all()

    def on_agent_state(self, ev: Any) -> None:
        state = getattr(ev, "new_state", None)
        speaking = state == "speaking"
        if speaking and not self._agent_speaking:
            self._pending += 1  # a new agent turn began speaking; its item will commit later
        self._agent_speaking = speaking
        logger.debug("transcript: agent_state=%s pending=%d", state, self._pending)

    # --- ordering core ---

    def _emit(self, ts: float, role: _Role, text: str) -> None:
        ts = max(ts, self._last_ts)  # never publish out of order, even on small clock skew
        self._last_ts = ts
        logger.debug("transcript: emit %s turn ts=%.3f", role, ts)  # role/ts only, never text
        self._queue.put_nowait((ts, role, text))

    def _hold(self, turn: _Turn) -> None:
        self._buffer.append(turn)
        self._buffer.sort(key=lambda t: t[0])
        self._arm_timeout()

    def _release_all(self) -> None:
        """Emit every held caller turn, in ts order (they all follow the agent turn that was
        just emitted)."""
        for ts, role, text in self._buffer:
            self._emit(ts, role, text)
        self._buffer.clear()
        self._arm_timeout()

    def _arm_timeout(self) -> None:
        if self._buffer and self._timeout_handle is None:
            self._timeout_handle = self._loop.call_later(self._hold_timeout, self._on_timeout)
        elif not self._buffer and self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

    def _on_timeout(self) -> None:
        self._timeout_handle = None
        logger.warning(
            "transcript reorder timeout for %s; flushing %d held turn(s)",
            self._room,
            len(self._buffer),
        )
        self._pending = 0  # a pending turn never committed; stop holding behind it (self-heal)
        self._release_all()

    # --- egress ---

    async def _drain(self) -> None:
        while True:
            turn = await self._queue.get()
            if turn is None:
                return
            ts, role, text = turn
            try:
                await self._service.publish_turn(self._room, role, text, ts=int(ts * 1000))
            except Exception as exc:  # best-effort; a Redis failure must not break the call
                logger.warning("transcript publish failed: %r", exc)

    async def aclose(self) -> None:
        """Flush held turns and drain the queue. Call before TranscriptService.end() so no
        turn is lost behind the ended sentinel."""
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None
        self._release_all()
        self._queue.put_nowait(None)  # stop the worker once it has drained what's queued
        await self._worker


def attach_transcript_publisher(
    session: Any, service: TranscriptService, room_name: str
) -> ReorderingEmitter:
    """Register session handlers that publish finalized user/agent turns in chronological
    order via `service`. Returns the emitter; `await emitter.aclose()` on shutdown (before
    `service.end`) to flush any turn still held for reordering."""
    emitter = ReorderingEmitter(service, room_name)
    session.on("user_input_transcribed", emitter.on_user)
    session.on("conversation_item_added", emitter.on_agent_item)
    session.on("agent_state_changed", emitter.on_agent_state)
    return emitter
