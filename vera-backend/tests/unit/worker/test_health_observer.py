"""CallHealthObserver: user-turn trigger, cold-start gate, cooldown coalescing,
single-in-flight, takeover stop (pre-start AND pre-emit), unassessable no-op,
LLM-failure skip, shutdown cancellation. The cascade must never notice it."""

import asyncio
from typing import Any

import pytest

from agent_worker.health_observer import CallHealthObserver
from vera_core.call_health import HealthTranscript
from vera_core.events import CallHealthEvent
from vera_core.observability.otel_testing import assert_no_phi_values


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None  # when set, complete() blocks on it
        self.closed = False

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.reply

    async def aclose(self) -> None:
        self.closed = True


class _FakeCallStream:
    def __init__(self) -> None:
        self.health: list[dict[str, Any]] = []

    async def publish_health(
        self, room_name: str, *, score: int, flag: str, reason: str, ts: int
    ) -> None:
        self.health.append({"room": room_name, "score": score, "flag": flag, "reason": reason})


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[CallHealthEvent] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


_OK_REPLY = (
    '{"assessable": true, "call_health_score": 80, "intervention_flag": "none", "reason": "ok"}'
)


def _observer(
    llm: _FakeLLM,
    stream: _FakeCallStream,
    bus: _FakeBus,
    *,
    engaged: bool = False,
    min_interval_s: float = 0.0,
    min_user_turns: int = 2,
) -> tuple[CallHealthObserver, dict[str, bool]]:
    state = {"engaged": engaged}
    obs = CallHealthObserver(
        room_name="room-x",
        llm=llm,
        call_stream=stream,  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        engaged=lambda: state["engaged"],
        transcript=HealthTranscript(max_turns=60),
        min_user_turns=min_user_turns,
        min_interval_s=min_interval_s,
    )
    return obs, state


async def _feed(obs: CallHealthObserver, *turns: tuple[str, str]) -> None:
    for role, text in turns:
        await obs.publish_turn("room-x", role, text, ts=1)  # type: ignore[arg-type]


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_analyzes_after_min_user_turns_and_emits_both_rails() -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "hello"), ("user", "hi"))
    await _settle()
    assert llm.calls == []  # 1 user turn < min_user_turns=2 — cold-start gate
    await _feed(obs, ("agent", "name?"), ("user", "jane"))
    await _settle()
    assert len(llm.calls) == 1
    assert stream.health and stream.health[0]["score"] == 80
    assert bus.events and bus.events[0].flag == "none" and bus.events[0].turn_count == 4
    await obs.aclose()
    assert llm.closed


@pytest.mark.asyncio
async def test_cooldown_coalesces_turn_burst_into_one_deferred_run() -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus, min_interval_s=0.15)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()  # ~0.2s
    first_count = len(llm.calls)
    assert first_count == 1  # first run immediate
    await _feed(obs, ("user", "e"), ("user", "f"), ("user", "g"))  # burst inside cooldown
    await asyncio.sleep(0.3)
    assert len(llm.calls) == 2  # exactly ONE deferred run for the whole burst
    await obs.aclose()


@pytest.mark.asyncio
async def test_takeover_stops_before_start_and_before_emit() -> None:
    # Pre-start: engaged before any analysis -> zero LLM calls, task exits.
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus, engaged=True)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()
    assert llm.calls == [] and stream.health == [] and bus.events == []
    await obs.aclose()

    # Pre-emit: takeover lands while the LLM call is in flight -> result discarded.
    llm2, stream2, bus2 = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm2.gate = asyncio.Event()
    obs2, state2 = _observer(llm2, stream2, bus2)
    await _feed(obs2, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if llm2.calls:
            break
    state2["engaged"] = True  # supervisor takes over mid-analysis
    llm2.gate.set()
    await _settle()
    assert stream2.health == [] and bus2.events == []
    await obs2.aclose()


@pytest.mark.asyncio
async def test_unassessable_and_llm_failure_are_silent_no_ops() -> None:
    llm, stream, bus = _FakeLLM('{"assessable": false}'), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()
    assert len(llm.calls) == 1 and stream.health == [] and bus.events == []
    await obs.aclose()

    llm2, stream2, bus2 = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm2.error = RuntimeError("providers down")
    obs2, _ = _observer(llm2, stream2, bus2)
    await _feed(obs2, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()  # must not raise anywhere
    assert stream2.health == [] and bus2.events == []
    await obs2.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_inflight_analysis() -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm.gate = asyncio.Event()  # never set — the LLM call hangs
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if llm.calls:
            break
    await obs.aclose()  # must return promptly, not hang on the in-flight call
    assert stream.health == [] and bus.events == []


@pytest.mark.asyncio
async def test_analysis_tags_the_llm_call_span(otel_spans: Any) -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "Patient name?"), ("user", "Jane Doe."))
    await _settle()
    await _feed(obs, ("agent", "Another?"), ("user", "Yes it's covered."))
    await _settle()
    span = next(
        s for s in otel_spans.get_finished_spans() if s.name == "vera.health_observer.llm_call"
    )
    assert span.attributes["vera.llm.purpose"] == "health_observer"
    # PHI guardrail (design §8): the rendered transcript is raw PHI — only the purpose
    # label rides on this span. Substring check, so an embedded value fails too.
    assert_no_phi_values(span, "Jane Doe")


@pytest.mark.asyncio
async def test_analysis_llm_call_span_does_not_record_exceptions(otel_spans: Any) -> None:
    # PHI guardrail: a provider error message can embed the prompt (raw transcript), so
    # NOTHING derived from the exception may reach the span. OTel has two independent knobs
    # and both must be off — record_exception=False drops the exception EVENT, and
    # set_status_on_exception=False drops the status description, which OTel would otherwise
    # fill with f"{type}: {exc}" (i.e. str(exc)) even with the event disabled. Before the
    # second kwarg was added, this message sat verbatim on span.status.description.
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm.error = RuntimeError("providers down: rejected prompt for Jane Doe")
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "Question?"), ("user", "Yes."))
    await _settle()
    await _feed(obs, ("agent", "Another?"), ("user", "Yes it's covered."))
    await _settle()
    span = next(
        s for s in otel_spans.get_finished_spans() if s.name == "vera.health_observer.llm_call"
    )
    assert not span.events  # record_exception=False — no exception event
    assert span.status.description is None  # set_status_on_exception=False — no str(exc)
    assert_no_phi_values(span, "Jane Doe", "providers down")
