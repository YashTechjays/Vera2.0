"""Cascade agents and the dispatch-metadata selector.

PlanTaskAgent (plan-driven, one per task) is the default verification path when the control
plane compiled a Call Plan for this call; VeraAgent (the static persona) is the fallback
when no plan exists (console / legacy v1). The generic IVR navigator (IvrNavigatorAgent,
`ivr_agent.py`) navigates the payer IVR; once it reaches a live rep it hands off to the
verification agent — the plan agent when a plan exists, else VeraAgent.
"""

import logging
from collections.abc import Callable

from livekit.agents import Agent, llm

from agent_worker.ivr_agent import IvrNavigatorAgent
from agent_worker.ivr_prompt import parse_ivr_playbook
from agent_worker.plan_agent import PlanTaskAgent
from agent_worker.plan_run_state import PlanRunState
from agent_worker.prompt import build_instructions, resolve_greeting
from vera_core.phi import PHIBoundaryProtocol

logger = logging.getLogger("agent_worker")


class VeraAgent(Agent):
    def __init__(
        self,
        boundary: PHIBoundaryProtocol,
        session_id: str,
        *,
        instructions: str | None = None,
        greeting: str | None = None,
    ) -> None:
        # Retained (unused today) for a future PHI seam re-add; see the removed PHIWallNodes.
        self._boundary = boundary
        self._session_id = session_id
        self._greeting = greeting if greeting is not None else resolve_greeting()
        super().__init__(
            instructions=instructions if instructions is not None else build_instructions(),
        )

    @llm.function_tool(
        name="end_call",
        description=(
            "End the phone call. Call this tool IMMEDIATELY after you say your "
            "closing line (e.g. 'thanks so much for your help, have a good one'). "
            "This hangs up the call for all participants."
        ),
    )
    async def _end_call(self) -> str:
        """Drain pending TTS audio then shut down the session."""
        self.session.shutdown(drain=True)
        return "Call ended."

    async def on_enter(self) -> None:
        self.session.say(self._greeting)


def _first_plan_agent(plan_state: PlanRunState) -> PlanTaskAgent:
    """The plan's entry agent — the first task in compile order (the sole place that rule lives)."""
    return PlanTaskAgent(plan_state, plan_state.plan.tasks[0].task_key)


def _verification_factory(
    plan_state: PlanRunState | None,
    boundary: PHIBoundaryProtocol,
    session_id: str,
    *,
    instructions: str | None,
    greeting: str | None,
) -> Callable[[], Agent]:
    """The agent an IVR navigator hands off to once a human answers: the plan's first task
    agent when a plan exists, otherwise the static VeraAgent."""
    if plan_state is not None:
        return lambda: _first_plan_agent(plan_state)
    return lambda: VeraAgent(boundary, session_id, instructions=instructions, greeting=greeting)


def build_agent(
    meta: dict[str, object],
    *,
    boundary: PHIBoundaryProtocol,
    session_id: str,
    instructions: str | None = None,
    greeting: str | None = None,
    plan_state: PlanRunState | None = None,
) -> Agent:
    """Pick the initial agent from dispatch metadata + whether a Call Plan was compiled.

    `enable_ivr_navigation` boots the IVR navigator (which hands off to the verification
    agent once a human answers). Otherwise, a compiled plan drives PlanTaskAgent (the
    default); with no plan the static VeraAgent runs (console / legacy fallback)."""
    if meta.get("enable_ivr_navigation"):
        return IvrNavigatorAgent(
            boundary,
            session_id,
            playbook=parse_ivr_playbook(meta),
            verification_factory=_verification_factory(
                plan_state, boundary, session_id, instructions=instructions, greeting=greeting
            ),
        )
    if meta.get("ivr_playbook") is not None:
        logger.warning("ivr_playbook present without enable_ivr_navigation; ignoring playbook")
    if plan_state is not None:
        return _first_plan_agent(plan_state)
    return VeraAgent(boundary, session_id, instructions=instructions, greeting=greeting)
