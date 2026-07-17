"""Observer runtime: transcript-stream tailing, rep-turn filtering, per-task isolation,
dedup, rotation drain, crash isolation, and the record → emit → apply-directive chain."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_worker.directives import Terminate
from agent_worker.observer import (
    ExtractedAnswer,
    ObserverManager,
    ResilientAnswerExtractor,
    _parse_extraction,
)
from vera_core.events.worker import CallAnswerRecordedEvent
from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor, PlanSession, PlanTask
from vera_core.forms.dsl import Comparison, FlowRule
from vera_core.llm import LLMUnavailableError
from vera_core.transcript import TranscriptEvent

ROOM = "call--t--c"


def _field(path: str) -> PlanFieldDescriptor:
    return PlanFieldDescriptor(path=path, title=path.split(".")[-1], type="text", role="ask")


def _plan(*, flow_rules: list[FlowRule] | None = None) -> CallPlan:
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(task_key="t1", title="T1", prompt=".", fields=[_field("sections.a.x")]),
            PlanTask(task_key="t2", title="T2", prompt=".", fields=[_field("sections.b.y")]),
        ],
        flow_rules=flow_rules or [],
    )


def _rep(text: str, ts: int = 1) -> TranscriptEvent:
    return TranscriptEvent(role="user", source="rep", text=text, ts=ts)


def _bot(text: str, ts: int = 1) -> TranscriptEvent:
    return TranscriptEvent(role="agent", source="bot", text=text, ts=ts)


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


class FakeTranscript:
    """A TranscriptSource whose read() yields queued items then RETURNS (end-of-call)."""

    def __init__(self, items: list[tuple[str, TranscriptEvent] | None] | None = None) -> None:
        self.items = items or []

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent] | None]:
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


async def _feed(manager: ObserverManager, event: TranscriptEvent) -> None:
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
    async def test_bot_turn_does_not_trigger_a_pass_but_counts_a_seq(self) -> None:
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _bot("Question?"))  # seq 0 — no extraction on a bot turn
        assert extractor.calls == 0
        await _feed(manager, _rep("Yes."))  # seq 1
        assert run_state.records[0][3] == 1  # evidence_seq = latest rep turn seq


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


class TestCrashIsolation:
    @pytest.mark.asyncio
    async def test_raising_extractor_does_not_break_the_call(self) -> None:
        extractor = FakeExtractor([], raises=True)
        manager, run_state, _, _ = _manager(_plan(), extractor)
        await _feed(manager, _rep("boom"))  # must not raise
        await manager.aclose()  # must not raise
        assert run_state.records == []


class FakeCompletionLLM:
    """Stands in for vera_core.llm.ResilientLLM's `complete` surface."""

    def __init__(self, reply: str = "[]", *, raises: bool = False) -> None:
        self.reply = reply
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.raises:
            raise LLMUnavailableError
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
    async def test_chain_exhausted_returns_empty(self) -> None:
        out = await ResilientAnswerExtractor(FakeCompletionLLM(raises=True)).extract(
            _plan().tasks[0], "anything"
        )
        assert out == []  # LLMUnavailableError → skip the pass, call continues


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
