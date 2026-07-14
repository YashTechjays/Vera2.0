"""Cascade agents.

The worker is PLAN-ONLY (2026-07-13): the compiled CallPlan is the sole
verification prompt source. The former monolithic SYSTEM_PROMPT / VeraAgent
fallback script is gone. VeraAgent is now just the end_call-carrying base for
the plan runtime's WrapUpAgent and for ApologyAgent.

PHI tokenization was dropped (2026-07-13): agents are plain LiveKit agents (no
stt/tts redact/hydrate seam).

`build_agent()` picks the initial persona from dispatch metadata; it requires a
`PlanRunController` (a real call always has a compiled plan). When no plan can be
built the entrypoint runs `ApologyAgent` instead — a graceful exit, never a
generic verification script. The IVR navigator (`ivr_agent.py`) hands off to the
plan's first task agent once a live rep answers.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from livekit.agents import Agent, llm

from agent_worker.ivr_agent import IvrNavigatorAgent
from agent_worker.ivr_prompt import parse_agent_context, parse_ivr_playbook

if TYPE_CHECKING:
    # TYPE_CHECKING-only: plan_runtime subclasses VeraAgent from this module,
    # so a runtime import here would be a cycle.
    from agent_worker.plan_runtime import PlanRunController

logger = logging.getLogger("agent_worker")

# Spoken when a call reaches a rep but has no usable plan (Redis miss / build
# failure). One polite line, then hang up — never the verification script.
APOLOGY_LINE = (
    "Hi, I'm so sorry — we're having a technical issue on our end and can't "
    "complete this verification right now. We'll call back. Thanks so much, and "
    "have a good one."
)


class VeraAgent(Agent):
    """Base agent carrying just the end_call tool, for an agent whose LLM ends
    the call by tool call (the plan runtime's WrapUpAgent). The monolithic Vera
    persona / greeting is gone — plan agents supply their own instructions."""

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


class ApologyAgent(Agent):
    """Graceful-exit agent for a call with no usable plan: speaks one fixed
    apology line, then hangs up. NOT a verification fallback — it collects
    nothing and runs no script. The LLM is bypassed (on_enter drives it
    deterministically), so the `instructions` string is inert."""

    def __init__(self) -> None:
        super().__init__(instructions="Say the apology line exactly once, then end the call.")

    async def on_enter(self) -> None:
        self.session.say(APOLOGY_LINE)
        self.session.shutdown(drain=True)


def build_agent(
    meta: dict[str, object],
    *,
    controller: "PlanRunController",
    on_keypress: Callable[[str], None] | None = None,
) -> Agent:
    """Pick the initial persona for a plan-backed call: the IVR navigator when
    `enable_ivr_navigation` is set (with an optional per-provider `ivr_playbook`
    overlay), else the plan's first task agent. The navigator hands off to the
    same first task agent once a live rep answers. The flag is the sole selector
    — a playbook without it is a producer inconsistency, logged and ignored.

    A `controller` is required: a real call always has a compiled CallPlan. The
    no-plan case is handled upstream (the entrypoint runs ApologyAgent), so this
    never falls back to a generic verification script."""
    if meta.get("enable_ivr_navigation"):
        return IvrNavigatorAgent(
            playbook=parse_ivr_playbook(meta),
            context=parse_agent_context(meta),
            on_keypress=on_keypress,
            verification_agent_factory=controller.first_agent,
        )
    if meta.get("ivr_playbook") is not None:
        logger.warning("ivr_playbook present without enable_ivr_navigation; ignoring playbook")
    if meta.get("agent_context") is not None:
        logger.warning("agent_context present without enable_ivr_navigation; ignoring")
    return controller.first_agent()
