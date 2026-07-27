"""Observer runtime: extract answers from the live transcript, one Observer per task.

A single ``ObserverManager`` tails the call-event Redis stream, filtering it to transcript
turns (decoupled from the voice pipeline — it reads the stream the emitter writes, it is
not a fan-out sink). It never runs
during the IVR phase or wrap-up: it routes each finalized turn to the Observer for
``controller.active_task_index``, and when that index is ``None`` (IVR, wrap-up) the turn is
dropped — extraction only happens on the conversation path.

Each task gets its OWN ``TaskObserver``, bound to exactly that task's field whitelist:
* It can only ever write its own task's fields — an answer for another task's field is
  dropped, so a handoff can never mis-attribute or lose an answer.
* On a task change the manager rotates: the outgoing Observer is closed with a final drain
  pass (catching a trailing turn finalized during the outro) while the incoming one takes
  over. The drain runs in the background so the turn pipeline is never blocked on an LLM call.

Side effects are centralized in the manager's ``record`` callback (the single answers
writer): ``run_state.record_answer`` → ``bus.emit`` → dedup → (on a rule fire)
``apply_directive_now`` redirects the live call. The call-scoped
``RuleEngine`` and the accumulated answers snapshot live on the manager, so a flow rule that
depends on an earlier task's answer still fires.

The whole runtime is best-effort: every extraction pass is wrapped so a raising LLM (or a
Redis blip) logs its type, kills that pass, and the call continues.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from opentelemetry import trace

from agent_worker.rule_engine import RuleEngine
from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.events.worker import CallAnswerRecordedEvent, WorkerEventBus
from vera_core.forms.call_plan import CallPlan, PlanTask
from vera_core.plan_store import PlanRunStateService
from vera_core.transcript import (
    SOURCE_BOT,
    SOURCE_REP,
    SOURCE_SUPERVISOR,
    resolve_turn_source,
)

if TYPE_CHECKING:
    from agent_worker.plan_runtime import PlanRunController

logger = logging.getLogger("agent_worker")
tracer = trace.get_tracer(__name__)

# Cap the transcript window fed to the extractor: the last N finalized turns of the
# current task. Bounds the prompt size and memory; a task rarely spans more.
_MAX_WINDOW_TURNS = 24


@dataclass(frozen=True, slots=True)
class ExtractedAnswer:
    field_path: str
    value: str
    confidence: int | None = None


@dataclass(frozen=True, slots=True)
class _Turn:
    # role/source are plain str: they arrive from the call-event envelope's JSON `data`,
    # and are only ever string-compared (SOURCE_REP) or label-looked-up here.
    role: str
    text: str
    source: str | None
    ts: int
    seq: int


class AnswerExtractor(Protocol):
    """Pulls answers for one task out of a rendered transcript window. Injectable so the
    Observer's routing/debounce/lifecycle is testable without a real Gemini call."""

    async def extract(self, task: PlanTask, transcript: str) -> list[ExtractedAnswer]: ...


type RecordFn = Callable[[ExtractedAnswer, int | None], Awaitable[None]]


class _CompletionLLM(Protocol):
    """The `vera_core.llm.ResilientLLM` surface the extractor needs — injectable so the
    extractor's own request/parse logic is testable without a real provider chain."""

    async def complete(self, *, system: str, user: str) -> str: ...


class ResilientAnswerExtractor:
    """Extraction via the out-of-pipeline fault-tolerant chain (Gemini primary → OpenAI
    fallback, `vera_core.llm.ResilientLLM`) — the mandated seam for non-cascade LLM calls.
    Strict JSON is enforced by the prompt + defensive parse (no provider JSON mode)."""

    def __init__(self, llm: _CompletionLLM) -> None:
        self._llm = llm

    async def extract(self, task: PlanTask, transcript: str) -> list[ExtractedAnswer]:
        # A whole-chain outage PROPAGATES rather than returning [], which is indistinguishable
        # from "the rep answered nothing" and would retire those turns unextracted.
        #
        # BOTH kwargs are required to keep a raised exception off the span: OTel's
        # Span.__exit__ has two independent knobs — record_exception=False drops the exception
        # EVENT (message + traceback), and set_status_on_exception=False drops the status
        # description, which is otherwise f"{type}: {exc}" and would still export str(exc).
        # The transcript passed to the chain is PHI, so neither may carry it.
        with tracer.start_as_current_span(
            "vera.observer.extraction_llm_call",
            attributes={"vera.llm.purpose": "observer_extraction", "vera.task.key": task.task_key},
            record_exception=False,
            set_status_on_exception=False,
        ):
            reply = await self._llm.complete(system=_extraction_instructions(task), user=transcript)
        return _parse_extraction(reply)


def _extraction_instructions(task: PlanTask) -> str:
    lines = [
        "You extract answers from a phone call between an insurance-verification agent and "
        "a payer representative. Return ONLY the fields below that the representative has "
        "clearly answered in the transcript. Output a JSON array of "
        '{"field_path", "value", "confidence"} (confidence 0-100). No prose, no code fence. '
        "Omit a field entirely if it is not yet answered. Use only these field_path values:",
    ]
    for f in task.fields:
        allowed = f" (one of: {', '.join(f.values)})" if f.values else ""
        lines.append(f"- {f.path}: {f.title}{allowed}")
    return "\n".join(lines)


def _parse_extraction(text: str) -> list[ExtractedAnswer]:
    """Tolerant strict-JSON parse: a bad payload skips the whole pass (returns [])."""
    payload = text.strip()
    if payload.startswith("```"):  # strip an accidental code fence
        payload = payload.strip("`").removeprefix("json").strip()
    try:
        rows = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    answers: list[ExtractedAnswer] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path, value = row.get("field_path"), row.get("value")
        if not isinstance(path, str) or value is None:
            continue
        answers.append(
            ExtractedAnswer(
                field_path=path,
                value=str(value),
                confidence=_clamp_confidence(row.get("confidence")),
            )
        )
    return answers


def _clamp_confidence(raw: Any) -> int | None:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return max(0, min(100, int(raw)))


class TaskObserver:
    """The extraction loop for ONE task. `feed` is synchronous and non-blocking; each rep
    turn schedules a debounced pass (coalesced single-flight while one is in flight)."""

    def __init__(
        self,
        task: PlanTask,
        *,
        whitelist: frozenset[str],
        extractor: AnswerExtractor,
        record: RecordFn,
    ) -> None:
        self._task = task
        self._whitelist = whitelist
        self._extractor = extractor
        self._record = record
        self._window: deque[str] = deque(maxlen=_MAX_WINDOW_TURNS)
        self._latest_rep_seq: int | None = None
        self._running = False
        self._pending = False
        self._closed = False
        self._dirty = False
        self._passes: set[asyncio.Task[None]] = set()

    def feed(self, turn: _Turn) -> None:
        if self._closed:
            return
        self._window.append(_render_turn(turn))
        self._dirty = True
        if turn.source == SOURCE_REP:
            # The REP's answer is the only new evidence worth a pass — keyed on source, not
            # role: under a takeover the supervisor also publishes as role=user, so its
            # question still enters the window as context, but it must not burn a pass nor
            # become this answer's evidence_seq.
            self._latest_rep_seq = turn.seq
            self._schedule_pass()

    def _schedule_pass(self) -> None:
        task = asyncio.create_task(self._run_passes())
        self._passes.add(task)
        task.add_done_callback(self._passes.discard)

    async def _run_passes(self) -> None:
        # Single-flight + coalesce: a pass arriving mid-flight just marks `_pending`; the
        # active runner loops until no more turns have queued up.
        if self._running:
            self._pending = True
            return
        self._running = True
        try:
            while True:
                self._pending = False
                await self._one_pass()
                if not self._pending:
                    return
        except Exception as exc:  # a raising LLM kills the pass, never the call
            logger.warning(
                "observer task %s: extraction pass failed (%s)",
                self._task.task_key,
                type(exc).__name__,
            )
        finally:
            self._running = False

    async def _one_pass(self) -> None:
        if not self._window or not self._dirty:
            return  # nothing new since the last pass — skip the redundant LLM call
        self._dirty = False
        transcript = "\n".join(self._window)
        rep_seq = self._latest_rep_seq
        try:
            extracted = await self._extractor.extract(self._task, transcript)
        except Exception:
            # Re-arm: the window still holds unextracted turns.
            self._dirty = True
            raise
        for answer in extracted:
            if answer.field_path not in self._whitelist:
                continue  # another task's field — never ours to write
            await self._record(answer, rep_seq)

    async def aclose(self) -> None:
        """Stop taking turns, drain in-flight passes, then run one final pass IF any turn
        arrived since the last pass (so a turn finalized just before the handoff is still
        extracted, without a redundant LLM call when nothing is new)."""
        self._closed = True
        while self._passes:
            await asyncio.gather(*list(self._passes), return_exceptions=True)
        try:
            await self._one_pass()
        except Exception as exc:
            logger.warning(
                "observer task %s: final drain failed (%s)",
                self._task.task_key,
                type(exc).__name__,
            )


_SPEAKER_LABELS = {
    SOURCE_REP: "Representative",
    SOURCE_BOT: "Agent",
    # Under a takeover the human supervisor asks the questions — label them distinctly so
    # the extractor reads them as questions, never as the rep's answers.
    SOURCE_SUPERVISOR: "Supervisor",
}


def _render_turn(turn: _Turn) -> str:
    speaker = _SPEAKER_LABELS.get(turn.source or "", turn.role)
    return f"{speaker}: {turn.text}"


class TranscriptSource(Protocol):
    """A tailable call-event stream — `RedisCallStreamStore` in production, a fake in tests.
    `read` replays from the start then blocks-and-tails, yielding `None` on an idle window
    and returning when the call ends (the end sentinel, or the stream key disappearing).
    The stream is mixed: transcript turns AND call_status frames (filtered in `ingest`)."""

    def read(
        self, room_name: str, *, first_entry_deadline_s: float | None = None
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]: ...


# Bound how long shutdown waits for the tail loop to drain to the end sentinel before it
# force-cancels (a crashed writer may never write the sentinel).
_TAIL_DRAIN_TIMEOUT_S = 5.0


class ObserverManager:
    """Tails the transcript Redis stream, routes each turn to the active task's Observer, and
    owns the call-scoped answer/rule state. It is NOT a fan-out sink — it reads the stream the
    emitter writes, decoupled from the voice pipeline, and filters to rep turns client-side."""

    def __init__(
        self,
        plan: CallPlan,
        *,
        controller: "PlanRunController",
        run_state: PlanRunStateService,
        bus: WorkerEventBus,
        extractor: AnswerExtractor,
        transcript: TranscriptSource,
        room_name: str,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._plan = plan
        self._controller = controller
        self._run_state = run_state
        self._bus = bus
        self._extractor = extractor
        self._transcript = transcript
        self._room = room_name
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._rule_engine = RuleEngine(plan)
        # Call-scoped answer snapshot (seeded with intake prefill), the dedup key and the
        # rule engine's input. Grows across tasks — a flow rule may span them.
        self._answers: dict[str, Any] = dict(plan.prefilled)
        self._seq = 0
        self._active_index: int | None = None
        self._active: TaskObserver | None = None
        self._closing: set[asyncio.Task[None]] = set()
        self._tail_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin tailing the transcript stream in the background."""
        self._tail_task = asyncio.create_task(self.run())

    async def run(self) -> None:
        """Tail the stream to end-of-call, feeding each turn to the active Observer. Returns
        when `read` returns (end sentinel / stream gone). A per-turn error keeps the loop
        alive; a fatal tail error kills observation but never the call."""
        try:
            async for item in self._transcript.read(self._room):
                if item is None:
                    continue  # idle keepalive tick
                try:
                    self.ingest(item[1])
                except Exception as exc:  # one bad turn must not stop the tail
                    logger.warning(
                        "observer manager %s: ingest failed (%s)", self._room, type(exc).__name__
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "observer manager %s: tail loop failed (%s)", self._room, type(exc).__name__
            )

    def ingest(self, event: CallStreamEvent) -> None:
        # Filter BEFORE anything else: a call_status frame must consume no seq slot AND must
        # not trigger a task rotation. Both skips mirror transcript_finalizer._build_rows, so
        # our seq stays equal to the row's eventual transcript.seq.
        if event.type != TYPE_TRANSCRIPT:
            return
        source = resolve_turn_source(event.data)
        if source is None:
            return  # unresolvable source — the finalizer drops it too, consuming no slot
        seq, self._seq = self._seq, self._seq + 1  # matches transcript.seq numbering
        index = self._controller.active_task_index
        if index != self._active_index:
            self._rotate(index)
        if self._active is not None:
            self._active.feed(
                _Turn(
                    role=str(event.data.get("role", "")),
                    text=str(event.data.get("text", "")),
                    source=source,
                    ts=event.ts,
                    seq=seq,
                )
            )

    def _rotate(self, index: int | None) -> None:
        if self._active is not None:
            self._schedule_close(self._active)  # background final drain, non-blocking
        self._active_index = index
        if index is None:
            self._active = None
            return
        task = self._plan.tasks[index]
        self._active = TaskObserver(
            task,
            whitelist=frozenset(f.path for f in task.fields),
            extractor=self._extractor,
            record=self._record,
        )

    def _schedule_close(self, observer: TaskObserver) -> None:
        task = asyncio.create_task(observer.aclose())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _record(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
        if self._answers.get(answer.field_path) == answer.value:
            # Unchanged — do not re-write or re-emit. INTENTIONALLY covers the intake
            # prefill seed too: a rep merely confirming a prefilled value leaves no
            # ai_call row (the INTAKE row stays current). The form reads the same either
            # way — only the answer's provenance differs.
            return
        ts = self._now_ms()
        await self._run_state.record_answer(
            self._room,
            answer.field_path,
            value=answer.value,
            ts=ts,
            confidence=answer.confidence,
            evidence_seq=evidence_seq,
        )
        await self._bus.emit(
            CallAnswerRecordedEvent(
                room_name=self._room,
                field_path=answer.field_path,
                value=answer.value,
                confidence=answer.confidence,
                evidence_seq=evidence_seq,
                ts=ts,
            )
        )
        # Mark dedup only after the write+emit land, so a failed emit is retried on the
        # next pass (the CP consumer is idempotent under the redelivery).
        self._answers[answer.field_path] = answer.value
        self._controller.update_answers(self._answers)
        # Same two-knob guardrail as the LLM-call spans (record_exception drops the exception
        # EVENT, set_status_on_exception drops the f"{type}: {exc}" status description): this
        # span wraps `self._answers`, raw extracted field values. `evaluate` is pure string
        # comparison and is documented not to raise, so this is defense-in-depth — but a
        # PHI-carrying object is right there, so both knobs stay off, as on every Vera-owned
        # span whose body touches PHI.
        with tracer.start_as_current_span(
            "vera.rule_engine.evaluate",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            directive = self._rule_engine.evaluate(self._answers)
            try:
                span.set_attribute("vera.rule_engine.fired", directive is not None)
                if directive is not None:
                    span.set_attribute("vera.handoff.directive_type", type(directive).__name__)
                    span.set_attribute("vera.handoff.rule_key", directive.rule_key)
            except Exception as exc:
                logger.warning(
                    "observer manager %s: rule-engine span tagging failed (%s)",
                    self._room,
                    type(exc).__name__,
                )
            if directive is not None:
                # Redirect the live call NOW: interrupt the bot + swap/re-ask (the controller
                # serializes it against an in-flight task_complete handoff).
                await self._controller.apply_directive_now(directive)

    async def aclose(self) -> None:
        """Stop tailing and drain. Call in the entrypoint shutdown AFTER the call-event
        stream's end() sentinel is written (so the tail drains the final turns) and BEFORE
        the plan-run state is cleared. The tail normally exits on the sentinel; bounded so a
        never-written sentinel can't hang shutdown."""
        cancelled = False
        if self._tail_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._tail_task), _TAIL_DRAIN_TIMEOUT_S)
            except TimeoutError:  # sentinel never came (crashed writer) — force-stop the tail
                self._tail_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._tail_task
            except asyncio.CancelledError:
                # aclose ITSELF was cancelled (wait_for unwraps the shield): stop the
                # tail, finish the drain below, then honor the cancellation.
                cancelled = True
                self._tail_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._tail_task
        self._rotate(None)  # close the active Observer with a final drain pass
        while self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)
        if cancelled:
            raise asyncio.CancelledError
