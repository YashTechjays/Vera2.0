"""A supervisor takeover must stop the plan from ending the call.

Silencing the agent only mutes its audio: the plan keeps advancing, reaches WrapUpAgent,
and its goodbye directive calls end_call — hanging up on a live human conversation.

The race test drives the takeover concurrently with the final handoff, so the invariant
holds for both interleavings and never depends on whether interrupt() cancels a tool call
already in flight.
"""

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import fakeredis
import pytest
from livekit.agents import Agent
from livekit.agents.llm import FunctionTool
from redis.asyncio import Redis

from agent_worker.intervention import AgentTakeoverController, TakeoverState
from agent_worker.plan_runtime import PlanRunController
from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask
from vera_core.plan_store import PlanRunStateService, RedisPlanRunStateStore

ROOM = "call--t--c"


def _plan() -> CallPlan:
    return CallPlan(
        schema_name="Infertility",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="p", goal="g", base_instructions="b"),
        tasks=[
            PlanTask(task_key="t1", title="T1", prompt="ask things"),
            PlanTask(task_key="t2", title="T2", prompt="ask more", outro="that's everything"),
        ],
    )


def _run_state() -> PlanRunStateService:
    redis = cast(Redis, fakeredis.aioredis.FakeRedis(decode_responses=True))
    return PlanRunStateService(RedisPlanRunStateStore(redis, ttl_seconds=100))


class _FakeAudio:
    def set_audio_enabled(self, enable: bool) -> None: ...


class _FakeSpeechHandle:
    """`say()` returns a handle; on_enter awaits its playout before leading on."""

    async def wait_for_playout(self) -> None: ...


class _FakeSession:
    """Records every way the agent could speak or hang up."""

    def __init__(self) -> None:
        self.userdata = TakeoverState()
        self.input = _FakeAudio()
        self.output = _FakeAudio()
        self.said: list[str] = []
        self.generate_reply_calls: list[str | None] = []
        self.shutdown_calls = 0

    def interrupt(self, *, force: bool = False) -> object:
        return None

    def say(self, text: str) -> object:
        self.said.append(text)
        return _FakeSpeechHandle()

    def generate_reply(self, *, instructions: str | None = None) -> object:
        self.generate_reply_calls.append(instructions)
        return None

    def shutdown(self, *, drain: bool = False) -> None:
        self.shutdown_calls += 1


def _tool(agent: Agent, name: str) -> Callable[[], Awaitable[Any]]:
    """The named tool, pre-bound with the `reason` every tool now requires — nothing in the
    runtime reads it, so no test here cares what it says."""
    tool = next(t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == name)
    return functools.partial(tool, reason="the task's questions are all answered")


def _controller() -> PlanRunController:
    return PlanRunController(_plan(), room_name=ROOM, run_state=_run_state())


@pytest.mark.asyncio
async def test_takeover_racing_the_final_handoff_never_ends_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    ctl = AgentTakeoverController(session)
    controller = _controller()
    # Patch on the base class so every plan agent sees the fake session.
    monkeypatch.setattr(Agent, "session", property(lambda self: session))

    last_task = controller.agents[-1]

    async def takeover() -> None:
        await asyncio.sleep(0)  # interleave inside the awaited handoff
        ctl.engage()

    result, _ = await asyncio.gather(_tool(last_task, "task_complete")(), takeover())

    # Either interleaving is legal. Speaking or hanging up is not.
    if isinstance(result, Agent):
        assert result is controller.wrap_up_agent
        await result.on_enter()  # the goodbye directive fires here, if anywhere

    assert session.generate_reply_calls == []
    assert session.shutdown_calls == 0  # the bug: never hang up under a takeover
    assert ctl.engaged is True
    await controller.drain_cursor_writes()


@pytest.mark.asyncio
async def test_task_complete_does_not_advance_once_taken_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    controller = _controller()
    monkeypatch.setattr(Agent, "session", property(lambda self: session))
    AgentTakeoverController(session).engage()

    result = await _tool(controller.agents[0], "task_complete")()

    assert not isinstance(result, Agent)  # a str tool-result → no handoff, plan parks
    assert controller.generation == 0
    assert session.said == []  # the outro would speak over the supervisor
    await controller.drain_cursor_writes()


@pytest.mark.asyncio
async def test_wrap_up_skips_the_goodbye_once_taken_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    controller = _controller()
    monkeypatch.setattr(Agent, "session", property(lambda self: session))
    AgentTakeoverController(session).engage()

    await controller.wrap_up_agent.on_enter()

    assert session.generate_reply_calls == []
    assert controller.active_task_index is None  # the cursor still records where we parked
    await controller.drain_cursor_writes()


@pytest.mark.asyncio
async def test_wrap_up_still_says_goodbye_without_a_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: the guard must not disable wrap-up on a normal call."""
    session = _FakeSession()
    controller = _controller()
    monkeypatch.setattr(Agent, "session", property(lambda self: session))

    await controller.wrap_up_agent.on_enter()

    assert len(session.generate_reply_calls) == 1
    await controller.drain_cursor_writes()
