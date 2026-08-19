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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from opentelemetry import trace

from agent_worker.rule_engine import RuleEngine
from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.events.worker import (
    CallAnswerRecordedEvent,
    CallRuleTerminatedEvent,
    WorkerEventBus,
)
from vera_core.forms.answers import canonical_answer, literals_of
from vera_core.forms.call_plan import CallPlan, PlanTask
from vera_core.forms.consistency import derive_remaining, triplet_paths
from vera_core.forms.extraction_prompt import (
    answer_shape_rules,
    is_coverage_status_path,
    special_values_hint,
)
from vera_core.forms.review import is_blank_answer
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
        # The transcript handed to the chain is PHI and a raised provider error can embed it,
        # so BOTH OTel exception knobs must be off: record_exception=False drops the exception
        # EVENT (message + traceback), set_status_on_exception=False drops the status
        # description, which OTel would otherwise fill with f"{type}: {exc}".
        with tracer.start_as_current_span(
            "vera.observer.extraction_llm_call",
            attributes={"vera.llm.purpose": "observer_extraction", "vera.task.key": task.task_key},
            record_exception=False,
            set_status_on_exception=False,
        ):
            reply = await self._llm.complete(system=_extraction_instructions(task), user=transcript)
        return _canonicalized(_parse_extraction(reply), task)


def _canonicalized(answers: list[ExtractedAnswer], task: PlanTask) -> list[ExtractedAnswer]:
    """Every answer spelled as its leaf declares it — see `canonical_answer`."""
    fields = {f.path: f for f in task.fields}
    return [
        replace(
            answer,
            value=canonical_answer(
                answer.value,
                literals_of(field) if (field := fields.get(answer.field_path)) else None,
            ),
        )
        for answer in answers
    ]


def _extraction_instructions(task: PlanTask) -> str:
    lines: list[str] = []
    names_exact = False
    for f in task.fields:
        # An enum's vocabulary is one clause, and deliberately NOT `literals_of().gate`: a rep
        # never states the `inapplicable_value` the either/or auto-fill writes. Every other
        # leaf's named answers are ALTERNATIVES to a normal answer, so they get "exact:".
        if f.values:
            vocabulary = f" (one of: {', '.join([*f.values, *(f.special_values or [])])})"
        else:
            vocabulary = special_values_hint(f.special_values)
            # The rule explains that clause, so only a task carrying one carries it (227 chars).
            names_exact = names_exact or bool(vocabulary)
        # Routing branches are not gated on the choice, so without this the extractor infers `No`
        # for the branch the rep did not take — a coverage claim, where `N/A` is the truth.
        note = f" — {f.exclusive_note}" if f.exclusive_note else ""
        # The PATH disambiguates a repeated title, so `owner_title` is not prefixed: measured
        # over three services with three different cycle limits it changed neither attribution
        # nor formatting, and cost ~1.3k chars on the CPT panel, re-sent every pass.
        lines.append(f"- {f.path}: {f.title}{vocabulary}{note}")
    # Same conditioning as the exact-value rule above: 250 chars on a prompt re-sent every pass.
    collects_coverage = any(is_coverage_status_path(f.path) for f in task.fields)
    preamble = (
        "You extract answers from a phone call between an insurance-verification agent and "
        "a payer representative. Return ONLY the fields below that the representative has "
        "clearly answered in the transcript. Output a JSON array of "
        '{"field_path", "value", "confidence"} (confidence 0-100). No prose, no code fence. '
        "Omit a field entirely if it is not yet answered. "
        f"{answer_shape_rules(names_exact=names_exact, collects_coverage=collects_coverage)} "
        "Use only these field_path values:"
    )
    return "\n".join([preamble, *lines])


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
        # blank/absent answers must not supersede the intake baseline (VR2-93)
        if not isinstance(path, str) or is_blank_answer(value):
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
        await self.drain_passes()
        try:
            await self._one_pass()
        except Exception as exc:
            logger.warning(
                "observer task %s: final drain failed (%s)",
                self._task.task_key,
                type(exc).__name__,
            )

    async def drain_passes(self) -> None:
        """Await in-flight passes WITHOUT closing — this observer stays live and keeps
        taking turns afterward. Used at a handoff: the manager's own rotation is lazy (it
        fires from the NEXT `ingest`), so the task that just finished is still `_active`,
        not yet `_retiring`, and its last answer may still be mid-extraction."""
        # Discard each batch BEFORE awaiting it: `add_done_callback` runs via call_soon, so an
        # already-finished pass stays in the set and a membership loop would spin forever.
        while self._passes:
            passes = list(self._passes)
            self._passes.difference_update(passes)
            await asyncio.gather(*passes, return_exceptions=True)


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

# Default bound for drain_pending, used when the caller (main.py) doesn't derive one from
# settings (see ObserverManager's drain_timeout param) — e.g. Voice Lab, tests. NOT sized off
# the 15.3s/23.2s completion-latency figures: drain_pending only waits on passes that already
# EXIST, so scheduling lag before a pass starts is either already spent or unreachable by any
# timeout here. It is sized off the extraction chain's own attempt cap
# (observer_extract_attempt_timeout_seconds, 8.0s today) plus adapter overhead — one in-flight
# attempt, not the whole fallback cascade. Kept tight because the gap pass CHAINS agent to
# agent with no speech between them, so every extra second here is silent dead air repeated
# once per swept task, not a one-time cost.
_DRAIN_TIMEOUT_S = 10.0


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
        drain_timeout: float | None = None,
    ) -> None:
        self._plan = plan
        self._controller = controller
        self._run_state = run_state
        self._bus = bus
        self._extractor = extractor
        self._transcript = transcript
        self._room = room_name
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        # `drain_pending`'s default when its caller doesn't pass one explicitly. main.py
        # derives this from the extraction chain's own attempt-timeout setting so the two
        # budgets can't drift apart; falls back to _DRAIN_TIMEOUT_S when unset (Voice Lab).
        self._drain_timeout = _DRAIN_TIMEOUT_S if drain_timeout is None else drain_timeout
        self._rule_engine = RuleEngine(plan)
        # Everything on file for this form — intake included, all roles. NOT the controller's
        # gate-evaluation set: three behaviours here need the intake values, namely the dedup in
        # `_record_locked`, `_derive_remaining_locked`'s "a prefilled remaining wins", and the
        # rule engine, whose terminate rules read ask-role paths a clinic may fill.
        # Snapped on the way in: the rule engine compares byte-exact, and a prefill written
        # before the writers canonicalized carries whatever spelling its source used.
        literals = {f.path: literals_of(f) for t in plan.tasks for f in t.fields}
        self._on_file: dict[str, Any] = {
            path: canonical_answer(value, literals.get(path))
            for path, value in plan.prefilled.items()
        }
        # What THIS CALL collected, which is all the controller may gate on — see
        # `PlanRunController.update_answers`.
        self._recorded: dict[str, Any] = {}
        self._triplets = [triplet_paths(rule.triplet) for rule in plan.numeric_consistencies]
        self._derived: dict[str, str] = {}
        self._seq = 0
        self._active_index: int | None = None
        self._active: TaskObserver | None = None
        # The task we just left, still taking turns for one more rep turn (see `_rotate`).
        self._retiring: TaskObserver | None = None
        # Background tasks a drain still owes a wait to: aclose() tasks from `_schedule_close`,
        # plus any drain_pending() batch member still pending past its timeout (kept here as a
        # strong ref so it isn't GC'd mid-flight, and so a later drain/aclose still awaits it).
        self._closing: set[asyncio.Task[None]] = set()
        self._tail_task: asyncio.Task[None] | None = None
        # A retiring Observer's pass runs concurrently with the active one's, and `_record`
        # read-modify-writes `_on_file` across awaits.
        self._record_lock = asyncio.Lock()
        self._rule_terminated_emitted = False

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
        turn = _Turn(
            role=str(event.data.get("role", "")),
            text=str(event.data.get("text", "")),
            source=source,
            ts=event.ts,
            seq=seq,
        )
        if self._active is not None:
            self._active.feed(turn)
        if self._retiring is not None:
            # The grace turn: only the outgoing task's Observer has that field on its
            # whitelist AND in its extractor prompt.
            self._retiring.feed(turn)
            if source == SOURCE_REP:
                self._close_retiring()  # had its grace turn

    def _rotate(self, index: int | None) -> None:
        if self._active is not None:
            # Retire rather than close: the turn that triggered this rotation may itself be
            # the answer to the outgoing task's final question (VERA asks and calls
            # `task_complete` in one turn), since the cursor read in `ingest` is a "now" value
            # about an arbitrarily stale turn. Bounded at one.
            self._close_retiring()
            self._retiring = self._active
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

    def _close_retiring(self) -> None:
        if self._retiring is not None:
            self._schedule_close(self._retiring)
            self._retiring = None

    def _schedule_close(self, observer: TaskObserver) -> None:
        task = asyncio.create_task(observer.aclose())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def drain_pending(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Await whatever extraction is still in flight before a caller reads the answer map.

        Three sources, because the manager's own rotation is LAZY (`_rotate` fires from the
        NEXT `ingest`, not from a cursor change): the task that just finished is still
        `_active`, not yet `_retiring`, so its last answer may be mid-pass right here; a
        prior handoff may have left an observer in `_retiring` with its grace turn not yet
        used; and `_closing` may hold aclose() tasks from an even earlier handoff.

        The gap sweep decides what is still owed; extraction lands ~15s after the turn that
        produced it (p90 23s), so without draining all three the sweep re-asks the last
        question the rep just answered. Bounded and best-effort: `asyncio.wait` (never
        `wait_for`/`asyncio.timeout`) so a timeout returns the caller WITHOUT cancelling the
        extraction it was waiting for — a cancelled pass is an answer lost for good, which is
        worse than the phantom gap this exists to fix.

        Every task this drains carries its OWN `add_done_callback(self._closing.discard)`,
        added at creation — none of it is removed from `_closing` up front, only `done`
        members after a wait that actually completed. So `drain_pending` being CANCELLED
        mid-wait (a hangup during the outro, session teardown) loses no tracking: everything
        stays in `_closing` for the next drain or the final `aclose` to pick up; the answer
        still lands, just later than this call waited."""
        self._close_retiring()
        batch: set[asyncio.Task[None]] = set(self._closing)
        if self._active is not None:
            drain = asyncio.create_task(self._active.drain_passes())
            self._closing.add(drain)
            drain.add_done_callback(self._closing.discard)
            batch.add(drain)
        if not batch:
            return
        bound = self._drain_timeout if timeout is None else timeout
        done, pending = await asyncio.wait(batch, timeout=bound)
        self._closing.difference_update(done)
        if pending:
            logger.warning("observer manager %s: drain timed out", self._room)

    async def _record(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
        async with self._record_lock:
            await self._record_locked(answer, evidence_seq)

    async def _record_locked(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
        if self._on_file.get(answer.field_path) == answer.value:
            # Unchanged — skip the write and the emit either way, so a rep merely confirming
            # a prefilled value still leaves no ai_call row (the INTAKE row stays current).
            # But the controller must still learn it: `gating_seed` drops ask-role prefills
            # from its baseline, so this is the only place left that can tell it the call
            # itself stated the value — otherwise the field is owed for the rest of the call.
            if self._recorded.get(answer.field_path) != answer.value:
                self._push_recorded(answer.field_path, answer.value)
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
        self._on_file[answer.field_path] = answer.value
        self._push_recorded(answer.field_path, answer.value)
        # Path, confidence and task only — never the value (PHI). Both knobs off for the
        # same reason as the rule-engine span below: this span's body sits beside raw
        # extracted values.
        with tracer.start_as_current_span(
            "vera.observer.answer_recorded",
            record_exception=False,
            set_status_on_exception=False,
        ) as answer_span:
            try:
                attrs: dict[str, str | int] = {"vera.field.path": answer.field_path}
                if self._active_index is not None:
                    attrs["vera.task.key"] = self._plan.tasks[self._active_index].task_key
                if answer.confidence is not None:
                    attrs["vera.field.confidence"] = answer.confidence
                answer_span.set_attributes(attrs)
            except Exception as exc:
                logger.warning(
                    "observer manager %s: answer-recorded span tagging failed (%s)",
                    self._room,
                    type(exc).__name__,
                )
        # This span's body reads `self._on_file`, raw extracted field values. `evaluate` is
        # pure string comparison and is documented not to raise, so the two knobs below are
        # defense-in-depth here — but they stay off, as on every Vera-owned span whose body
        # touches PHI: record_exception=False drops the exception EVENT,
        # set_status_on_exception=False drops the f"{type}: {exc}" status description.
        with tracer.start_as_current_span(
            "vera.rule_engine.evaluate",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            directive = self._rule_engine.evaluate(self._on_file)
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
                await self._emit_rule_terminated_once(directive.rule_key)
        await self._derive_remaining_locked(answer, evidence_seq)

    async def _emit_rule_terminated_once(self, rule_key: str) -> None:
        """Persist the rule-terminated fact the moment the controller accepts it — the
        call.ended flag is only the shutdown-path echo and a crash would lose it."""
        if self._rule_terminated_emitted or not self._controller.ended_by_flow_rule:
            return
        try:
            await self._bus.emit(
                CallRuleTerminatedEvent(room_name=self._room, rule_key=rule_key, ts=self._now_ms())
            )
            self._rule_terminated_emitted = True
        except Exception as exc:  # best-effort: the call.ended flag is the backstop
            logger.warning(
                "observer manager %s: rule-terminated emit failed (%s)",
                self._room,
                type(exc).__name__,
            )

    def _push_recorded(self, field_path: str, value: str) -> None:
        """Tell the controller the call collected this value — the one sync point onto
        `_baseline`, which `gating_seed` may have excluded this path from entirely."""
        self._recorded[field_path] = value
        self._controller.update_answers(self._recorded)

    async def _derive_remaining_locked(
        self, trigger: ExtractedAnswer, evidence_seq: int | None
    ) -> None:
        """Fill a triplet's blank remaining with total - met (fill-gaps-only)."""
        for total_path, met_path, remaining_path in self._triplets:
            if trigger.field_path not in (total_path, met_path):
                continue
            current = self._on_file.get(remaining_path)
            if not is_blank_answer(current) and current != self._derived.get(remaining_path):
                continue  # a rep-stated or prefilled remaining wins — never overwrite it
            value = derive_remaining(
                str(self._on_file.get(total_path) or ""),
                str(self._on_file.get(met_path) or ""),
            )
            if value is None:
                continue
            self._derived[remaining_path] = value
            await self._record_locked(
                ExtractedAnswer(remaining_path, value, trigger.confidence), evidence_seq
            )

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
        self._rotate(None)  # retires the active Observer
        self._close_retiring()  # final drain pass for whatever is left
        while self._closing:
            closing = list(self._closing)
            self._closing.difference_update(closing)  # same call_soon race as TaskObserver.aclose
            await asyncio.gather(*closing, return_exceptions=True)
        if cancelled:
            raise asyncio.CancelledError
