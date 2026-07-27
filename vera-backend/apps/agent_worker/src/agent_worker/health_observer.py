"""Concurrent per-call health observer — never blocks the cascade.

An extra TurnPublisher sink on the job's fan-out: it accumulates the ordered
turn stream in a bounded, cache-friendly window and, on each completed USER
turn (bot spoke, rep replied), runs at most one in-flight LLM analysis with a
minimum interval between runs. Assessable results go out on two rails: a
`health` envelope on the per-call event stream (the live SSE) and a
`call.health` worker event (the control plane persists it). Everything here is
best-effort — an LLM outage or Redis failure logs a type name and skips the
cycle; the call never notices.

Stops permanently the moment a supervisor takeover engages (checked before
starting a run AND again before emitting a completed one — an in-flight result
must not land after a human stepped in), and is cancelled at job shutdown.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from opentelemetry import trace

from agent_worker.intervention import takeover_engaged
from vera_core.call_health import HEALTH_SYSTEM_PROMPT, HealthTranscript, parse_assessment
from vera_core.call_stream import CallStreamService
from vera_core.config import EnvSecretProvider, Settings
from vera_core.events import CallHealthEvent, WorkerEventBus
from vera_core.llm import FallbackOptions, LLMSpec, ResilientLLM
from vera_core.transcript import ROLE_AGENT, ROLE_USER, TurnRole, TurnSource, source_for_role

logger = logging.getLogger("agent_worker")
tracer = trace.get_tracer(__name__)


class _HealthLLM(Protocol):
    """Structural view of ResilientLLM (keeps tests decoupled)."""

    async def complete(self, *, system: str, user: str) -> str: ...
    async def aclose(self) -> None: ...


class CallHealthObserver:
    """See module docstring. Owns its LLM chain (aclose closes it)."""

    def __init__(
        self,
        *,
        room_name: str,
        llm: _HealthLLM,
        call_stream: CallStreamService,
        bus: WorkerEventBus,
        engaged: Callable[[], bool],
        transcript: HealthTranscript,
        min_user_turns: int,
        min_interval_s: float,
    ) -> None:
        self._room = room_name
        self._llm = llm
        self._call_stream = call_stream
        self._bus = bus
        self._engaged = engaged
        self._transcript = transcript
        self._min_user_turns = min_user_turns
        self._min_interval_s = min_interval_s
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._closed = False
        self._last_run = float("-inf")
        self._user_turns = 0
        self._agent_turns = 0
        self._task: asyncio.Task[None] = self._loop.create_task(self._run())

    # --- TurnPublisher sink (called by the fan-out; must stay cheap) ---

    async def publish_turn(
        self,
        room_name: str,
        role: TurnRole,
        text: str,
        *,
        ts: int,
        source: TurnSource | None = None,
        user_id: str | None = None,
    ) -> None:
        if self._closed:
            return
        self._transcript.add(role, source or source_for_role(role), text)
        if role == ROLE_AGENT:
            self._agent_turns += 1
        elif role == ROLE_USER:
            self._user_turns += 1
            # Trigger only on a completed exchange (bot spoke AND the rep has
            # replied enough times) — the cold-start gate (spec edge #11).
            if self._user_turns >= self._min_user_turns and self._agent_turns >= 1:
                self._wake.set()

    # --- analysis loop ---

    async def _run(self) -> None:
        while True:
            try:
                await self._wake.wait()
                # Cooldown: a turn burst inside the window coalesces into exactly
                # one deferred run (the event stays set until cleared below).
                wait_s = self._last_run + self._min_interval_s - self._loop.time()
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                self._wake.clear()
                if self._closed:
                    return
                if self._engaged():
                    logger.info("health observer for %s stopping: takeover engaged", self._room)
                    return  # permanent — its purpose is handing off to a human
                self._last_run = self._loop.time()
                await self._analyze_once()
            except asyncio.CancelledError:
                raise  # aclose()'s cancellation must still stop the loop
            except Exception as exc:
                # A stray exception here must not silently kill the task — it would
                # sit unretrieved until aclose()'s `await self._task` re-raises it,
                # skipping the LLM chain's own cleanup. Type name only: any future
                # code added to this loop may touch transcript content.
                logger.warning(
                    "health observer loop for %s hit %s; continuing",
                    self._room,
                    type(exc).__name__,
                )

    async def _analyze_once(self) -> None:
        user_message = self._transcript.render_user_message()
        turn_count = self._transcript.turn_count
        try:
            with tracer.start_as_current_span(
                "vera.health_observer.llm_call",
                attributes={"vera.llm.purpose": "health_observer"},
            ):
                reply = await self._llm.complete(system=HEALTH_SYSTEM_PROMPT, user=user_message)
        except Exception as exc:  # prompt/reply are PHI — type name only
            logger.warning("health analysis for %s skipped (%s)", self._room, type(exc).__name__)
            return
        result = parse_assessment(reply)
        if result is None:
            return  # unassessable / contract-ignoring reply — complete no-op
        if self._closed or self._engaged():
            return  # a late result must never land after shutdown/takeover
        ts = int(time.time() * 1000)
        await self._emit_best_effort(
            "frame publish",
            self._call_stream.publish_health(
                self._room, score=result.score, flag=result.flag, reason=result.reason, ts=ts
            ),
        )
        await self._emit_best_effort(
            "event emit",
            self._bus.emit(
                CallHealthEvent(
                    room_name=self._room,
                    score=result.score,
                    flag=result.flag,
                    reason=result.reason,
                    turn_count=turn_count,
                    ts=ts,
                )
            ),
        )

    async def _emit_best_effort(self, what: str, coro: Awaitable[None]) -> None:
        """Fire one output rail. A Redis/bus failure logs a type name (never the
        PHI payload) and is swallowed — the call never notices."""
        try:
            await coro
        except Exception as exc:
            logger.warning("health %s failed for %s (%s)", what, self._room, type(exc).__name__)

    async def aclose(self) -> None:
        """Idempotent: stop the loop (cancelling any in-flight analysis) and
        close the owned LLM chain."""
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        with contextlib.suppress(Exception):
            await self._llm.aclose()


def build_health_observer(
    session: Any,
    *,
    room_name: str,
    settings: Settings,
    call_stream: CallStreamService,
    bus: WorkerEventBus,
) -> CallHealthObserver:
    """Wire an observer for a real /calls job. `session` is the AgentSession —
    its TakeoverState userdata is the stop signal. The LLM chain is per-job and
    lazy (no provider client until the first analysis)."""
    llm = ResilientLLM(
        LLMSpec.parse(settings.health_primary_model),
        [LLMSpec.parse(selector) for selector in settings.health_fallback_models],
        options=FallbackOptions(attempt_timeout=settings.health_attempt_timeout_seconds),
        secrets=EnvSecretProvider(),
    )
    return CallHealthObserver(
        room_name=room_name,
        llm=llm,
        call_stream=call_stream,
        bus=bus,
        engaged=lambda: takeover_engaged(session),
        transcript=HealthTranscript(max_turns=settings.health_max_turns),
        min_user_turns=settings.health_min_user_turns,
        min_interval_s=settings.health_min_interval_seconds,
    )
