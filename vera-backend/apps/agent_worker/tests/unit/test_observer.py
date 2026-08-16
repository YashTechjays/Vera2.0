"""Observer runtime: transcript-stream tailing, rep-turn filtering, per-task isolation,
dedup, rotation drain, crash isolation, and the record → emit → apply-directive chain."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_worker.directives import ReAsk, Terminate
from agent_worker.observer import (
    ExtractedAnswer,
    ObserverManager,
    ResilientAnswerExtractor,
    _parse_extraction,
    _render_turn,
    _Turn,
)
from vera_core.call_stream import TYPE_CALL_STATUS, TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.events.worker import CallAnswerRecordedEvent
from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor, PlanSession, PlanTask
from vera_core.forms.dsl import Comparison, FlowRule, NumericConsistency
from vera_core.llm import LLMUnavailableError
from vera_core.observability.otel_testing import assert_no_phi_values

ROOM = "call--t--c"


def _field(path: str) -> PlanFieldDescriptor:
    return PlanFieldDescriptor(path=path, title=path.split(".")[-1], type="text", role="ask")


def _plan(
    fields: list[PlanFieldDescriptor] | None = None,
    *,
    flow_rules: list[FlowRule] | None = None,
    numeric_consistencies: list[NumericConsistency] | None = None,
    prefilled: dict[str, Any] | None = None,
) -> CallPlan:
    # `fields` overrides the default two-task shape with a single task ("t1") whitelisting
    # exactly those fields — for tests that only care about one task's extraction/whitelist.
    tasks = (
        [PlanTask(task_key="t1", title="T1", prompt=".", fields=fields)]
        if fields is not None
        else [
            PlanTask(
                task_key="t1",
                title="T1",
                prompt=".",
                fields=[
                    _field("sections.a.x"),
                    _field("sections.a.total"),
                    _field("sections.a.met_amount"),
                    _field("sections.a.remaining"),
                ],
            ),
            PlanTask(task_key="t2", title="T2", prompt=".", fields=[_field("sections.b.y")]),
        ]
    )
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=tasks,
        flow_rules=flow_rules or [],
        numeric_consistencies=numeric_consistencies or [],
        prefilled=prefilled or {},
    )


def _turn(role: str, source: str, text: str, ts: int = 1) -> CallStreamEvent:
    return CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": role, "source": source, "text": text}, ts=ts
    )


def _rep(text: str, ts: int = 1) -> CallStreamEvent:
    return _turn("user", "rep", text, ts)


def _bot(text: str, ts: int = 1) -> CallStreamEvent:
    return _turn("agent", "bot", text, ts)


def _supervisor(text: str, ts: int = 1) -> CallStreamEvent:
    # Under a takeover the TakeoverTranscriber publishes the supervisor as role=user.
    return _turn("user", "supervisor", text, ts)


def _status(status: str = "active", ts: int = 1) -> CallStreamEvent:
    return CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": status}, ts=ts)


class FakeExtractor:
    """Returns a fixed answer list per call; `raises` makes a pass blow up."""

    def __init__(self, answers: list[ExtractedAnswer], *, raises: bool = False) -> None:
        self.answers = answers
        self.raises = raises
        self.calls = 0

    async def extract(self, task: Any, transcript: str) -> list[ExtractedAnswer]:
        self.calls += 1
        if self.raises:
            raise RuntimeError("gemini exploded")
        return list(self.answers)


class SlowExtractor(FakeExtractor):
    """`FakeExtractor` that takes `delay` seconds, so a drain has something to wait for."""

    def __init__(self, answers: list[ExtractedAnswer], *, delay: float) -> None:
        super().__init__(answers)
        self.delay = delay

    async def extract(self, task: Any, transcript: str) -> list[ExtractedAnswer]:
        await asyncio.sleep(self.delay)
        return await super().extract(task, transcript)


class FakeTranscript:
    """A TranscriptSource whose read() yields queued items then RETURNS (end-of-call)."""

    def __init__(self, items: list[tuple[str, CallStreamEvent] | None] | None = None) -> None:
        self.items = items or []

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent] | None]:
        for item in self.items:
            yield item


class FakeRunState:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, Any, int | None]] = []

    async def record_answer(
        self,
        room: str,
        field_path: str,
        *,
        value: Any,
        ts: int,
        confidence: int | None = None,
        evidence_seq: int | None = None,
    ) -> None:
        self.records.append((room, field_path, value, evidence_seq))


class FakeBus:
    def __init__(self) -> None:
        self.events: list[CallAnswerRecordedEvent] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class FakeController:
    def __init__(self) -> None:
        self.active_task_index: int | None = 0
        self.answers: dict[str, Any] = {}
        self.applied: list[Any] = []

    def update_answers(self, answers: dict[str, Any]) -> None:
        self.answers = dict(answers)

    async def apply_directive_now(self, directive: Any) -> None:
        self.applied.append(directive)


def _manager(
    plan: CallPlan,
    extractor: FakeExtractor,
    *,
    transcript: FakeTranscript | None = None,
) -> tuple[ObserverManager, FakeRunState, FakeBus, FakeController]:
    run_state, bus, controller = FakeRunState(), FakeBus(), FakeController()
    manager = ObserverManager(
        plan,
        controller=controller,  # type: ignore[arg-type]
        run_state=run_state,  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        extractor=extractor,
        transcript=transcript or FakeTranscript(),
        room_name=ROOM,
        now_ms=lambda: 111,
    )
    return manager, run_state, bus, controller


async def _feed(manager: ObserverManager, event: CallStreamEvent) -> None:
    manager.ingest(event)
    await _settle()


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class TestRecording:
    @pytest.mark.asyncio
    async def test_whitelisted_answer_records_emits_and_updates(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, bus, controller = _manager(_plan(), extractor)
        await _feed(manager, _rep("It is yes."))
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 0)]  # rep turn is seq 0
        assert len(bus.events) == 1 and bus.events[0].field_path == "sections.a.x"
        assert bus.events[0].value == "Yes"
        assert controller.answers["sections.a.x"] == "Yes"

    @pytest.mark.asyncio
    async def test_foreign_field_is_dropped_not_written(self) -> None:
        # task t1 owns sections.a.x only; an extractor emitting t2's field must be ignored
        extractor = FakeExtractor([ExtractedAnswer("sections.b.y", "leak", 90)])
        manager, run_state, bus, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("unrelated"))
        assert run_state.records == []
        assert bus.events == []

    @pytest.mark.asyncio
    async def test_unchanged_value_is_recorded_once(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, bus, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("yes"))
        await _feed(manager, _rep("still yes"))
        assert len(run_state.records) == 1
        assert len(bus.events) == 1

    @pytest.mark.asyncio
    async def test_confirming_an_ask_role_prefill_still_reaches_the_controller(self) -> None:
        # sections.a.x is `ask`-role (see `_field`), which `gating_seed` drops from the
        # controller's baseline — the dedup branch below is the only place left that can
        # still tell the controller the call stated it.
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Family", 90)])
        manager, run_state, bus, controller = _manager(
            _plan(prefilled={"sections.a.x": "Family"}), extractor
        )
        await _feed(manager, _rep("It's family coverage."))
        assert controller.answers["sections.a.x"] == "Family"
        # No new ai_call row and no emit — a mere confirmation leaves the INTAKE row current.
        assert run_state.records == []
        assert bus.events == []

    @pytest.mark.asyncio
    async def test_bot_turn_does_not_trigger_a_pass_but_counts_a_seq(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _bot("Question?"))  # seq 0 — no extraction on a bot turn
        assert extractor.calls == 0
        await _feed(manager, _rep("Yes."))  # seq 1
        assert run_state.records[0][3] == 1  # evidence_seq = latest rep turn seq


class TestAnswerRecordedSpan:
    """Path, confidence and task only — never the value — fixing a call that recorded 233
    answers and exposed zero field paths in its trace."""

    @pytest.mark.asyncio
    async def test_a_recorded_answer_emits_a_span_naming_the_path_but_not_the_value(
        self, otel_spans: Any
    ) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, _, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("yes it is"))
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.observer.answer_recorded"
        )
        assert span.attributes["vera.field.path"] == "sections.a.x"
        assert span.attributes["vera.field.confidence"] == 90
        assert span.attributes["vera.task.key"] == "t1"
        # PHI must never reach a span (design's per-span denylist check): substring-checked
        # against the span name, every attribute VALUE, status description and events — not
        # merely "the path is present but the value happens not to collide with it".
        assert_no_phi_values(span, "Yes")
        assert "Yes" not in str(span.attributes)

    @pytest.mark.asyncio
    async def test_confidence_is_omitted_rather_than_coerced_when_absent(
        self, otel_spans: Any
    ) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", None)])
        manager, _, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("yes it is"))
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.observer.answer_recorded"
        )
        assert "vera.field.confidence" not in span.attributes

    @pytest.mark.asyncio
    async def test_task_key_is_omitted_when_no_task_is_active(self, otel_spans: Any) -> None:
        # `_active_index` is only ever None while no TaskObserver is feeding turns, so this
        # is not reachable through the normal ingest path — exercised directly on the
        # write-path method instead.
        manager, _, _, _ = _manager(_plan(), FakeExtractor([]))
        manager._active_index = None
        await manager._record_locked(ExtractedAnswer("sections.a.x", "Yes", 90), None)
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.observer.answer_recorded"
        )
        assert "vera.task.key" not in span.attributes


class TestStreamFiltering:
    """The Observer shares one mixed stream with the SSE/finalizer — non-transcript frames
    must be invisible to it (no pass, no seq slot, no task rotation)."""

    @pytest.mark.asyncio
    async def test_call_status_frame_is_ignored_entirely(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _status("active"))
        assert extractor.calls == 0
        assert run_state.records == []
        # …and it consumed no seq slot: the next rep turn is still seq 0.
        await _feed(manager, _rep("Yes."))
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 0)]

    @pytest.mark.asyncio
    async def test_unresolvable_source_turn_is_skipped(self) -> None:
        # Mirrors the finalizer dropping a corrupt envelope — no slot consumed either.
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _turn("???", "???", "junk"))
        assert extractor.calls == 0
        await _feed(manager, _rep("Yes."))
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 0)]


class TestSupervisorTakeover:
    """Form filling must keep working once a human supervisor drives the call: the
    TakeoverTranscriber publishes BOTH the supervisor and the rep as role=user, so the
    rep's answer (not the supervisor's question) is what triggers extraction."""

    @pytest.mark.asyncio
    async def test_supervisor_question_is_context_only(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _supervisor("Is the deductible met?"))
        assert extractor.calls == 0  # a supervisor turn must not burn a pass
        assert run_state.records == []

    @pytest.mark.asyncio
    async def test_rep_answer_after_takeover_still_extracts(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, bus, _ = _manager(_plan(), extractor)
        await _feed(manager, _supervisor("Is the deductible met?"))  # seq 0, context
        await _feed(manager, _rep("Yes, fully met."))  # seq 1, the evidence
        assert extractor.calls == 1
        # recorded, and evidence_seq points at the REP's turn — not the supervisor's
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 1)]
        assert len(bus.events) == 1

    def test_supervisor_turns_are_labelled_distinctly(self) -> None:
        # The extractor must be able to tell who asked from who answered.
        rendered = _render_turn(
            _Turn(role="user", text="Is it met?", source="supervisor", ts=1, seq=0)
        )
        assert rendered == "Supervisor: Is it met?"


class TestRouting:
    @pytest.mark.asyncio
    async def test_no_observer_runs_during_ivr_or_wrap_up(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, controller = _manager(_plan(), extractor)
        controller.active_task_index = None  # IVR / wrap-up
        await _feed(manager, _rep("nobody home"))
        assert extractor.calls == 0
        assert run_state.records == []

    @pytest.mark.asyncio
    async def test_rotation_routes_answers_to_the_new_task(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.b.y", "B", 90)])
        manager, run_state, _, controller = _manager(_plan(), extractor)
        controller.active_task_index = 1  # now on task t2
        await _feed(manager, _rep("answer for b"))
        assert run_state.records == [(ROOM, "sections.b.y", "B", 0)]  # rep turn is seq 0

    @pytest.mark.asyncio
    async def test_close_skips_final_pass_when_nothing_new(self) -> None:
        # The last pass already covered the window → close must not spend a redundant
        # LLM call on an identical transcript.
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, _, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("yes"))
        assert extractor.calls == 1
        await manager.aclose()
        assert extractor.calls == 1  # no redundant final drain

    @pytest.mark.asyncio
    async def test_aclose_reraises_an_outer_cancellation(self) -> None:
        # Cancelling shutdown mid-drain must clean up AND honor the cancellation —
        # not swallow it and report a clean close.
        class HangingTranscript:
            async def read(self, room_name: str) -> AsyncIterator[None]:
                while True:
                    yield None
                    await asyncio.sleep(0)

        manager, _, _, _ = _manager(
            _plan(),
            FakeExtractor([]),
            transcript=HangingTranscript(),  # type: ignore[arg-type]
        )
        manager.start()
        closer = asyncio.create_task(manager.aclose())
        await _settle()  # let aclose enter the bounded wait on the hanging tail
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer

    @pytest.mark.asyncio
    async def test_close_drains_a_trailing_turn(self) -> None:
        # No rep turn triggers a pass, but a turn is buffered; aclose's final pass extracts it.
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _bot("Only an agent line."))
        await manager.aclose()
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", None)]


class EvidenceBoundExtractor:
    """Answers only when the rep's line is in the window it was handed, so a lost turn cannot
    hide behind the outgoing Observer draining a question-only window (as it can with
    FakeExtractor, which answers from any transcript)."""

    def __init__(self, evidence: str, answer: ExtractedAnswer) -> None:
        self._evidence = evidence
        self._answer = answer
        self.calls = 0

    async def extract(self, task: Any, transcript: str) -> list[ExtractedAnswer]:
        self.calls += 1
        return [self._answer] if self._evidence in transcript else []


class TestHandoffGrace:
    """The outgoing Observer keeps receiving turns for one more rep turn before it closes,
    because a turn is attributed by the controller's LIVE cursor but reaches the manager
    arbitrarily late (hold buffer → queue → Redis → tail) — so when VERA asks and calls
    `task_complete` in one turn, the answer would land under the next task, off its
    whitelist, and be silently dropped (P1)."""

    @pytest.mark.asyncio
    async def test_answer_arriving_after_the_handoff_is_still_extracted(self) -> None:
        extractor = EvidenceBoundExtractor(
            "Yes, x is covered.", ExtractedAnswer("sections.a.x", "Yes", 90)
        )
        manager, run_state, _, controller = _manager(_plan(), extractor)  # type: ignore[arg-type]
        await _feed(manager, _bot("Is x covered?"))  # seq 0 — asked by t1
        controller.active_task_index = 1  # task_complete swapped in that same turn
        await _feed(manager, _rep("Yes, x is covered."))  # seq 1 — arrives under t2
        await _settle()
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 1)]

    @pytest.mark.asyncio
    async def test_grace_lasts_exactly_one_rep_turn(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, _, _, controller = _manager(_plan(), extractor)
        controller.active_task_index = 1
        await _feed(manager, _rep("the straddling answer"))
        await _settle()
        after_grace = extractor.calls
        await _feed(manager, _rep("a later turn, t2's alone"))
        await _settle()
        assert extractor.calls == after_grace + 1  # only t2's Observer ran

    @pytest.mark.asyncio
    async def test_window_stays_one_deep_across_back_to_back_rotations(self) -> None:
        # Bounded by design, which is why P10 (doubled `task_complete`) must land first: two
        # rotations with no rep turn between them retire two tasks, and only the most recent
        # one can still catch a straddling answer.
        extractor = FakeExtractor(
            [ExtractedAnswer("sections.a.x", "Yes", 90), ExtractedAnswer("sections.b.y", "B", 90)]
        )
        manager, run_state, _, controller = _manager(_plan(), extractor)
        await _feed(manager, _bot("Is x covered?"))
        controller.active_task_index = 1
        await _feed(manager, _bot("And now for b?"))
        controller.active_task_index = 0  # gap pass walks backwards
        await _feed(manager, _rep("b is fine."))
        await _settle()
        recorded = {r[1] for r in run_state.records}
        assert "sections.b.y" in recorded  # the most recently retired task still catches it

    @pytest.mark.asyncio
    async def test_aclose_drains_a_retiring_observer_that_never_got_its_grace_turn(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, controller = _manager(_plan(), extractor)
        await _feed(manager, _bot("Is x covered?"))  # buffered, no pass yet
        controller.active_task_index = 1
        await _feed(manager, _bot("Now for b."))  # rotation; no rep turn ever arrives
        await manager.aclose()
        assert (ROOM, "sections.a.x", "Yes", None) in run_state.records


class TestTailLoop:
    @pytest.mark.asyncio
    async def test_tail_reads_stream_filters_and_records(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        transcript = FakeTranscript(
            [
                ("0-0", _bot("What is the deductible?")),  # seq 0, no pass
                None,  # idle keepalive tick — ignored
                ("1-0", _rep("It's met.")),  # seq 1 → extraction
            ]
        )
        manager, run_state, _, _ = _manager(_plan(), extractor, transcript=transcript)
        manager.start()
        await manager.aclose()  # tail ends when the source returns; then drains
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 1)]

    @pytest.mark.asyncio
    async def test_aclose_without_start_is_safe(self) -> None:
        manager, _, _, _ = _manager(_plan(), FakeExtractor([]))
        await manager.aclose()  # no tail task, nothing buffered — must not raise


class TestRuleIntervention:
    @pytest.mark.asyncio
    async def test_recorded_answer_that_fires_a_rule_is_applied_immediately(self) -> None:
        flow = FlowRule(
            rule_key="stop",
            when=Comparison(field="sections.a.x", op="eq", value="No"),
            action="terminate_call",
        )
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "No", 90)])
        manager, _, _, controller = _manager(_plan(flow_rules=[flow]), extractor)
        await _feed(manager, _rep("the answer is no"))
        assert controller.applied == [Terminate(rule_key="stop")]

    @pytest.mark.asyncio
    async def test_fired_rule_tags_the_evaluate_span(self, otel_spans: Any) -> None:
        flow = FlowRule(
            rule_key="stop",
            when=Comparison(field="sections.a.x", op="eq", value="No"),
            action="terminate_call",
        )
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "No", 90)])
        manager, _, _, _ = _manager(_plan(flow_rules=[flow]), extractor)
        await _feed(manager, _rep("the answer is no"))
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.rule_engine.evaluate"
        )
        assert span.attributes["vera.rule_engine.fired"] is True
        assert span.attributes["vera.handoff.directive_type"] == "Terminate"
        assert span.attributes["vera.handoff.rule_key"] == "stop"
        # PHI guardrail (design §6/§8): the answer value that fired this rule ("No") must
        # never reach the span — only the enum/key metadata above. Substring check, so an
        # attribute that merely EMBEDS the value (e.g. "answer: No") fails too.
        assert_no_phi_values(span, "No")

    @pytest.mark.asyncio
    async def test_non_firing_evaluation_is_still_visible(self, otel_spans: Any) -> None:
        flow = FlowRule(
            rule_key="stop",
            when=Comparison(field="sections.a.x", op="eq", value="No"),
            action="terminate_call",
        )
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, _, _, _ = _manager(_plan(flow_rules=[flow]), extractor)
        await _feed(manager, _rep("the answer is yes"))
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.rule_engine.evaluate"
        )
        assert span.attributes["vera.rule_engine.fired"] is False
        assert "vera.handoff.directive_type" not in span.attributes
        assert_no_phi_values(span, "Yes")


class TestCrashIsolation:
    @pytest.mark.asyncio
    async def test_raising_extractor_does_not_break_the_call(self) -> None:
        extractor = FakeExtractor([], raises=True)
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("boom"))  # must not raise
        await manager.aclose()  # must not raise
        assert run_state.records == []

    @pytest.mark.asyncio
    async def test_failed_pass_is_retried_by_the_final_drain(self) -> None:
        # A provider outage on a task's LAST rep turn used to lose that answer for good: the
        # pass cleared `_dirty` before extracting, so the aclose() drain saw "nothing new"
        # and skipped the very turn it exists to catch. The failed pass must re-arm.
        class _FlakyExtractor:
            def __init__(self) -> None:
                self.calls = 0

            async def extract(self, task: Any, transcript: str) -> list[ExtractedAnswer]:
                self.calls += 1
                if self.calls == 1:
                    raise LLMUnavailableError  # whole chain exhausted, this pass only
                return [ExtractedAnswer("sections.a.x", "Yes", 90)]

        extractor = _FlakyExtractor()
        manager, run_state, _, _ = _manager(_plan(), extractor)  # type: ignore[arg-type]
        await _feed(manager, _rep("Yes, it is covered."))
        assert run_state.records == []  # first pass died in the outage

        await manager.aclose()
        assert extractor.calls == 2  # the drain RETRIED rather than skipping the window
        assert run_state.records == [(ROOM, "sections.a.x", "Yes", 0)]


class FakeCompletionLLM:
    """Stands in for vera_core.llm.ResilientLLM's `complete` surface."""

    def __init__(self, reply: str = "[]", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.reply


class TestResilientExtractor:
    @pytest.mark.asyncio
    async def test_completion_is_parsed_into_answers(self) -> None:
        reply = '[{"field_path": "sections.a.x", "value": "Yes", "confidence": 90}]'
        llm = FakeCompletionLLM(reply)
        out = await ResilientAnswerExtractor(llm).extract(_plan().tasks[0], "Representative: yes")
        assert out == [ExtractedAnswer("sections.a.x", "Yes", 90)]
        # the task's fields drive the system prompt, the window is the user message
        assert "sections.a.x" in llm.calls[0][0]
        assert llm.calls[0][1] == "Representative: yes"

    @pytest.mark.asyncio
    async def test_chain_exhausted_propagates(self) -> None:
        # It must NOT return [] — that is indistinguishable from "the rep answered nothing",
        # which would let the caller retire the window as extracted. The raise is what lets
        # TaskObserver re-arm and retry those turns (see test_failed_pass_is_retried_...).
        with pytest.raises(LLMUnavailableError):
            await ResilientAnswerExtractor(FakeCompletionLLM(error=LLMUnavailableError())).extract(
                _plan().tasks[0], "anything"
            )

    @pytest.mark.asyncio
    async def test_extract_tags_the_llm_call_span(self, otel_spans: Any) -> None:
        reply = '[{"field_path": "sections.a.x", "value": "Yes", "confidence": 90}]'
        llm = FakeCompletionLLM(reply)
        await ResilientAnswerExtractor(llm).extract(
            _plan().tasks[0], "Representative: yes, Jane Doe is covered."
        )
        span = next(
            s
            for s in otel_spans.get_finished_spans()
            if s.name == "vera.observer.extraction_llm_call"
        )
        assert span.attributes["vera.llm.purpose"] == "observer_extraction"
        assert span.attributes["vera.task.key"] == "t1"
        # PHI guardrail (design §8): the transcript window handed to the chain is raw PHI —
        # none of it may ride along on the span.
        assert_no_phi_values(span, "Jane Doe")

    @pytest.mark.asyncio
    async def test_extract_llm_call_span_does_not_record_exceptions(self, otel_spans: Any) -> None:
        # PHI guardrail: a provider error message can embed the prompt/transcript, so nothing
        # derived from the exception may reach the span — both OTel knobs asserted below.
        # The exception must CARRY a message for this to test anything: LLMUnavailableError is
        # always raised bare (str() == ""), so it would pass even unguarded. `complete` is a
        # Protocol, and an unexpected provider/SDK error escaping it can embed the request body.
        llm = FakeCompletionLLM(error=RuntimeError("provider rejected prompt for Jane Doe"))
        with pytest.raises(RuntimeError):
            await ResilientAnswerExtractor(llm).extract(_plan().tasks[0], "anything")
        span = next(
            s
            for s in otel_spans.get_finished_spans()
            if s.name == "vera.observer.extraction_llm_call"
        )
        assert not span.events  # record_exception=False — no exception event
        assert span.status.description is None  # set_status_on_exception=False — no str(exc)
        assert_no_phi_values(span, "Jane Doe")


class TestParsing:
    def test_parses_plain_json_array(self) -> None:
        out = _parse_extraction('[{"field_path": "a.b", "value": "Yes", "confidence": 88}]')
        assert out == [ExtractedAnswer("a.b", "Yes", 88)]

    def test_strips_code_fence(self) -> None:
        out = _parse_extraction('```json\n[{"field_path": "a.b", "value": "Yes"}]\n```')
        assert out == [ExtractedAnswer("a.b", "Yes", None)]

    def test_bad_json_skips_the_pass(self) -> None:
        assert _parse_extraction("not json at all") == []

    def test_clamps_confidence_and_coerces_value(self) -> None:
        out = _parse_extraction('[{"field_path": "a.b", "value": 500, "confidence": 250}]')
        assert out == [ExtractedAnswer("a.b", "500", 100)]

    def test_blank_values_are_dropped(self) -> None:  # VR2-93
        out = _parse_extraction(
            '[{"field_path": "a.b", "value": ""},'
            ' {"field_path": "a.c", "value": "   "},'
            ' {"field_path": "a.d", "value": "No"}]'
        )
        assert out == [ExtractedAnswer("a.d", "No", None)]


TRIPLET_RULE = NumericConsistency(rule_key="a_triplet", triplet="sections.a")
TOTAL, MET, REMAINING = "sections.a.total", "sections.a.met_amount", "sections.a.remaining"


class TestDerivedRemaining:
    @pytest.mark.asyncio
    async def test_derives_remaining_when_total_and_met_are_recorded(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 80)]
        )
        manager, run_state, bus, controller = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("Total is 25k, met 5k."))
        recorded = {path: value for _, path, value, _ in run_state.records}
        assert recorded[REMAINING] == "$20,000.00"
        assert controller.answers[REMAINING] == "$20,000.00"
        derived_events = [e for e in bus.events if e.field_path == REMAINING]
        assert len(derived_events) == 1
        assert derived_events[0].confidence == 80  # inherited from the triggering answer
        assert controller.applied == []  # a derived value never fires the triplet's ReAsk

    @pytest.mark.asyncio
    async def test_rep_stated_remaining_wins_and_stops_derivation(self) -> None:
        extractor = FakeExtractor(
            [
                ExtractedAnswer(TOTAL, "$25,000", 90),
                ExtractedAnswer(MET, "$5,000", 90),
                ExtractedAnswer(REMAINING, "$20,000", 90),
            ]
        )
        manager, _, _, controller = _manager(_plan(numeric_consistencies=[TRIPLET_RULE]), extractor)
        await _feed(manager, _rep("All three amounts."))
        assert controller.answers[REMAINING] == "$20,000"  # spoken value is current
        # A later Met correction must NOT recompute over the rep-stated value.
        extractor.answers = [ExtractedAnswer(MET, "$6,000", 90)]
        await _feed(manager, _rep("Correction: met is 6k.", ts=2))
        assert controller.answers[REMAINING] == "$20,000"

    @pytest.mark.asyncio
    async def test_recomputes_when_inputs_change_and_remaining_is_still_derived(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 90)]
        )
        manager, _, _, controller = _manager(_plan(numeric_consistencies=[TRIPLET_RULE]), extractor)
        await _feed(manager, _rep("Total 25k, met 5k."))
        assert controller.answers[REMAINING] == "$20,000.00"
        extractor.answers = [ExtractedAnswer(MET, "$6,000", 90)]
        await _feed(manager, _rep("Correction: met is 6k.", ts=2))
        assert controller.answers[REMAINING] == "$19,000.00"

    @pytest.mark.asyncio
    async def test_prefilled_remaining_blocks_derivation(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 90)]
        )
        manager, run_state, _, controller = _manager(
            _plan(
                numeric_consistencies=[TRIPLET_RULE],
                prefilled={REMAINING: "$1,000"},
            ),
            extractor,
        )
        await _feed(manager, _rep("Total 25k, met 5k."))
        # Blocked derivation means nothing was ever RECORDED for it, and the Observer now
        # pushes only what the call collected — the controller never sees the prefill either.
        assert REMAINING not in controller.answers
        assert not any(path == REMAINING for _, path, _, _ in run_state.records)

    @pytest.mark.asyncio
    async def test_impossible_inputs_derive_nothing_and_reask_fires(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$100", 90), ExtractedAnswer(MET, "$300", 90)]
        )
        manager, run_state, _, controller = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("Total 100, met 300."))
        assert not any(path == REMAINING for _, path, _, _ in run_state.records)
        assert any(isinstance(d, ReAsk) for d in controller.applied)

    @pytest.mark.asyncio
    async def test_repeated_passes_record_the_derived_value_once(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 90)]
        )
        manager, run_state, _, _ = _manager(_plan(numeric_consistencies=[TRIPLET_RULE]), extractor)
        await _feed(manager, _rep("Total 25k, met 5k."))
        await _feed(manager, _rep("Same again.", ts=2))
        derived = [r for r in run_state.records if r[1] == REMAINING]
        assert len(derived) == 1


def test_extraction_instructions_carry_the_routing_note() -> None:
    """Without it the extractor writes `No` for the branch the rep did not take."""
    from agent_worker.observer import _extraction_instructions
    from vera_core.forms.call_plan import PlanFieldDescriptor, PlanTask

    task = PlanTask(
        task_key="t",
        title="T",
        prompt="p",
        fields=[
            PlanFieldDescriptor(
                path="sections.s.branch_a.covered",
                title="Covered",
                type="enum",
                role="ask",
                values=["Yes", "No", "N/A"],
                exclusive_note="Only one of A or B applies. …record N/A here — never No…",
            ),
            PlanFieldDescriptor(
                path="sections.s.plain",
                title="Plain",
                type="text",
                role="ask",
            ),
        ],
    )
    text = _extraction_instructions(task)
    assert "record N/A here — never No" in text
    assert "- sections.s.plain: Plain" in text  # unnoted fields keep their bare line
    # The unit convention is ONE header sentence, not a per-field annotation — the leaf
    # titles already carry "(%)"/"($)", and annotating each field would multiply the cost
    # on a CPT panel. (The bare-line assertion above is what pins that down.) No declared
    # range is passed either: a "0-100" hint nudges the model into rescaling a fraction
    # like 0.2 into 20%, which is an upstream ambiguity we refuse to guess at.
    assert '"20%", never "20"' in text


class TestDrainPending:
    @pytest.mark.asyncio
    async def test_drain_awaits_the_still_active_observers_in_flight_pass(self) -> None:
        """The real call site: `note_task_entered` then `drain_observer`, with no transcript
        turn in between. `_rotate` is LAZY (it fires from the NEXT `ingest`), so the task that
        just finished is still `_active` here — never `_retiring`. Draining only
        `_retiring`/`_closing` (the fix's first cut) misses this pass entirely, and the sweep
        re-asks a question the rep just answered."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=0.05)
        manager, run_state, _bus, _controller = _manager(_plan([_field("sections.a.b")]), extractor)
        manager.ingest(_rep("yes it is"))  # schedules a pass on the ACTIVE observer
        await manager.drain_pending(timeout=5.0)
        assert run_state.records, "the active observer's in-flight pass was not drained"

    @pytest.mark.asyncio
    async def test_drain_awaits_the_retiring_observers_final_pass(self) -> None:
        """The shutdown-adjacent case: by the time `aclose()` retires the active observer
        (`_rotate(None)`), a straddling answer sits in `_retiring`, not `_active`. Still a
        real path (final call teardown), just not the one the gap sweep hits."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=0.05)
        manager, run_state, _bus, _controller = _manager(_plan([_field("sections.a.b")]), extractor)
        manager.ingest(_rep("yes it is"))
        manager._rotate(None)  # task ended; the outgoing observer retires
        await manager.drain_pending(timeout=5.0)
        assert run_state.records, "the final extraction pass had not completed when drain returned"

    @pytest.mark.asyncio
    async def test_drain_returns_on_timeout_instead_of_stalling(self) -> None:
        """The barrier must never become a hang: a slow extractor falls through."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=0.2)
        manager, _run_state, _bus, _controller = _manager(
            _plan([_field("sections.a.b")]), extractor
        )
        manager.ingest(_rep("yes it is"))
        manager._rotate(None)
        await asyncio.wait_for(manager.drain_pending(timeout=0.05), timeout=1.0)

    @pytest.mark.asyncio
    async def test_drain_timeout_does_not_cancel_the_pending_extraction(self) -> None:
        """`asyncio.timeout` cancels on expiry, and that cancellation cascades through
        `gather` into every awaited child — including the extraction pass itself, which is
        strictly worse than the phantom gap this barrier exists to fix (the answer is not
        late, it is gone). `asyncio.wait` never cancels: the pass keeps running after
        `drain_pending` returns, and its answer still lands, just later."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=0.1)
        manager, run_state, _bus, _controller = _manager(_plan([_field("sections.a.b")]), extractor)
        manager.ingest(_rep("yes it is"))
        manager._rotate(None)
        await manager.drain_pending(timeout=0.02)  # times out well before the 0.1s pass ends
        assert run_state.records == [], "should not have landed yet at this point"
        await asyncio.sleep(0.15)  # let the still-running pass finish in the background
        assert run_state.records, "the pending extraction was cancelled, not just delayed"

    @pytest.mark.asyncio
    async def test_drain_does_not_wedge_the_loop_on_already_done_closing_entries(self) -> None:
        """Regression fence for the hazard `TaskObserver.aclose` already guards against (see
        its comment): gathering already-completed tasks without removing them from the
        tracked set first can resolve WITHOUT ever yielding to the loop, so a `while
        self._closing:` loop around it spins forever rather than merely lingering. Constructs
        the state directly (a done task in `_closing` with no done_callback to discard it) so
        the repro does not depend on winning a callback-timing race."""

        async def _noop() -> None:
            return None

        done_task = asyncio.create_task(_noop())
        await done_task  # already completed; never wired to a callback that discards it
        manager, _run_state, _bus, _controller = _manager(_plan(), FakeExtractor([]))
        manager._closing.add(done_task)

        ticked = False

        async def _tick() -> None:
            nonlocal ticked
            await asyncio.sleep(0)
            ticked = True

        ticker = asyncio.create_task(_tick())
        await asyncio.wait_for(manager.drain_pending(timeout=1.0), timeout=1.0)
        await ticker
        assert ticked, "a concurrent task never got a turn — the loop was starved"
        assert done_task not in manager._closing

    @pytest.mark.asyncio
    async def test_cancelling_drain_pending_does_not_orphan_closing(self) -> None:
        """A drain cancelled mid-wait (hangup during the outro, session teardown) must not
        strand the adopted task outside `_closing`: removing it from the tracked set up
        front, before the await, is exactly what a cancellation skips past — the
        `if pending:` restore line never runs. Each task now carries its OWN discard
        callback and is only ever removed after a wait that actually completed, so a
        cancelled drain leaves it there for `aclose` to keep waiting on."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=0.1)
        manager, run_state, _bus, _controller = _manager(_plan([_field("sections.a.b")]), extractor)
        manager.ingest(_rep("yes it is"))
        manager._rotate(None)  # retires the observer; drain_pending will schedule its close
        drain = asyncio.create_task(manager.drain_pending(timeout=5.0))
        await asyncio.sleep(0)  # let drain_pending run its setup and reach the await
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
        assert manager._closing, "the adopted close task was stranded outside _closing"
        await manager.aclose()
        assert run_state.records, "aclose stopped waiting for the in-flight pass"
