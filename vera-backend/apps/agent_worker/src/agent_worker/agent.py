"""Cascade agents.

The worker is PLAN-ONLY (2026-07-13) for real calls: the compiled CallPlan is the
sole verification prompt source, and a real dispatched call that can't load a plan
FAILS FAST (the entrypoint hangs up; the shutdown callback re-dispatches). VeraAgent
is now just the end_call-carrying base for the plan runtime's WrapUpAgent.

PHI tokenization was dropped (2026-07-13): agents are plain LiveKit agents (no
stt/tts redact/hydrate seam).

`VoiceLabAgent` is the ONE non-plan agent: the Voice Lab preview sandbox dispatches
with no PatientForm and therefore no CallPlan, so it runs this generic conversational
persona instead of hanging up. It never serves a real dispatched call.

`build_agent()` picks the initial agent from dispatch metadata: the IVR navigator when
`enable_ivr_navigation` is set, else the "verification" agent — the plan's first task
agent for a plan-backed call, or a `VoiceLabAgent` when there is no controller (Voice
Lab preview). The navigator hands off to that same verification agent once a live rep
answers.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from livekit.agents import Agent, llm

from agent_worker.intervention import takeover_engaged
from agent_worker.ivr_agent import IvrNavigatorAgent
from agent_worker.ivr_prompt import parse_agent_context, parse_ivr_playbook
from agent_worker.prompt import (
    TOOL_REASON_ARG,
    build_voice_lab_instructions,
    resolve_voice_lab_greeting,
)
from agent_worker.tool_log import log_tool_reason
from vera_core.schemas import PersonaTweak

if TYPE_CHECKING:
    # TYPE_CHECKING-only: plan_runtime subclasses VeraAgent from this module,
    # so a runtime import here would be a cycle.
    from agent_worker.plan_runtime import PlanRunController

logger = logging.getLogger("agent_worker")


class VeraAgent(Agent):
    """Base agent carrying just the end_call tool, for an agent whose LLM ends
    the call by tool call (the plan runtime's WrapUpAgent). The monolithic Vera
    persona / greeting is gone — plan agents supply their own instructions."""

    @llm.function_tool(
        name="end_call",
        description=(
            "End the phone call. Call this tool IMMEDIATELY after you say your "
            "closing line (e.g. 'thanks so much for your help, have a good one'). "
            "This hangs up the call for all participants. " + TOOL_REASON_ARG
        ),
    )
    async def _end_call(self, reason: str) -> str | None:
        """Drain pending TTS audio then shut down the session.

        `reason` drives nothing here — it lands on the tool-call item for the eval harness, and
        goes to the log through `log_tool_reason`, which prints it verbatim only under
        VERA_LOG_TOOL_REASONS (it is model-authored text about live call state, so it is treated
        as PHI-bearing and production sees its length alone)."""
        log_tool_reason("end_call", reason)
        if takeover_engaged(self.session):
            # Reachable via a tool call already in flight when engage() interrupted us.
            logger.info("end_call refused: supervisor has taken over the call")
            return (
                "This call has been taken over by a human supervisor and will not be "
                "ended. Do not speak and do not call any more tools."
            )
        self.close_call()
        return None

    def close_call(self) -> None:
        """Hang up, letting queued speech (a closing outro) finish playing first."""
        self.session.shutdown(drain=True)


class VoiceLabAgent(VeraAgent):
    """Conversational agent for the Voice Lab preview sandbox (no CallPlan). Carries
    the inherited end_call tool, speaks a greeting on enter, and runs on the supplied
    generic persona `instructions`. A plain LiveKit agent — no PHI redact/hydrate seams
    (this branch dropped them) and no plan machinery."""

    def __init__(self, *, instructions: str, greeting: str) -> None:
        self._greeting = greeting
        super().__init__(instructions=instructions)

    async def on_enter(self) -> None:
        self.session.say(self._greeting)


def build_agent(
    meta: dict[str, object],
    *,
    controller: "PlanRunController | None",
    tweak: PersonaTweak | None = None,
    on_keypress: Callable[[str], None] | None = None,
) -> Agent:
    """Pick the initial agent from dispatch metadata: the IVR navigator when
    `enable_ivr_navigation` is set (with an optional per-provider `ivr_playbook`
    overlay), else the verification agent directly. The navigator hands off to that
    same verification agent once a live rep answers. The flag is the sole selector
    — a playbook without it is a producer inconsistency, logged and ignored.

    The verification agent is the plan's first task agent when a `controller` is
    present (a real plan-backed call), or a `VoiceLabAgent` on the generic preview
    persona when it is None (a Voice Lab sandbox session, which has no CallPlan)."""

    def make_verification_agent() -> Agent:
        if controller is not None:
            return controller.first_agent()
        return VoiceLabAgent(
            instructions=build_voice_lab_instructions(tweak),
            greeting=resolve_voice_lab_greeting(tweak),
        )

    if meta.get("enable_ivr_navigation"):
        return IvrNavigatorAgent(
            playbook=parse_ivr_playbook(meta),
            context=parse_agent_context(meta),
            on_keypress=on_keypress,
            verification_agent_factory=make_verification_agent,
        )
    if meta.get("ivr_playbook") is not None:
        logger.warning("ivr_playbook present without enable_ivr_navigation; ignoring playbook")
    if meta.get("agent_context") is not None:
        logger.warning("agent_context present without enable_ivr_navigation; ignoring")
    return make_verification_agent()
