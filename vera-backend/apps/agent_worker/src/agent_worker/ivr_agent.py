"""IVR navigator agent and its turn-handling config.

`IvrNavigatorAgent` (see the class) is the generic IVR navigator: a plain agent with no
PHI-wall node overrides that navigates the payer's IVR and hands off to `VeraAgent` — a
one-way swap — once a live human rep answers (from then on the PHI-wall overrides apply).
`ivr_turn_handling()` is the navigator's per-agent turn config: patient end-of-turn detection
for the IVR phase that reverts to the snappy human default at the handoff.
"""

import functools
import logging

from livekit.agents import (
    Agent,
    StopResponse,
    TurnHandlingOptions,
    function_tool,
    get_job_context,
    llm,
)
from livekit.plugins.turn_detector.english import EnglishModel

from agent_worker.dtmf import DtmfTransportError, InvalidDtmfError, send_dtmf
from agent_worker.ivr_prompt import build_ivr_instructions
from vera_core.config.settings import get_settings
from vera_core.phi import PHIBoundaryProtocol

logger = logging.getLogger("agent_worker")

# Deterministic backstop: if the navigator takes this many IVR turns without reaching a
# human, it hangs up rather than looping forever (enforced in on_user_turn_completed).
_IVR_MAX_TURNS = 60


def ivr_turn_handling() -> TurnHandlingOptions:
    """Fresh `turn_handling` for the IVR navigator (pass as `Agent(turn_handling=...)`).

    The endpointing delays are the key IVR-patience tunable; they live in settings so they
    can be adjusted without a code change (see `Settings.ivr_endpointing_*`).
    """
    settings = get_settings()
    return {
        "endpointing": {
            "min_delay": settings.ivr_endpointing_min_delay,
            "max_delay": settings.ivr_endpointing_max_delay,
        },
        "preemptive_generation": {"enabled": True},
        "turn_detection": EnglishModel(),
        "interruption": {
            "mode": "vad",
            "enabled": True,
            "min_words": 3,
            "false_interruption_timeout": 2.0,
            "resume_false_interruption": True,
        },
    }


class IvrNavigatorAgent(Agent):
    """Generic IVR navigator: the payer's IVR talks first, so the navigator stays silent
    on enter (default no-op on_enter) and responds prompt-by-prompt, speaking menu choices
    and pressing keypad digits (DTMF) via the `press_keypad` tool. Runs as a plain agent —
    no PHI-wall node overrides.

    It holds a factory for its VeraAgent handoff target so that, once a live human rep
    answers, `transfer_to_verification` can hand off to a VeraAgent (which greets the rep
    and runs the benefits conversation)."""

    def __init__(
        self,
        boundary: PHIBoundaryProtocol,
        session_id: str,
        *,
        verification_instructions: str | None = None,
        verification_greeting: str | None = None,
    ) -> None:
        # Deferred import breaks the agent <-> ivr_agent import cycle: agent.py imports
        # IvrNavigatorAgent, and the navigator only needs VeraAgent at construction time.
        from agent_worker.agent import VeraAgent

        # The navigator runs plain (no PHI-wall overrides); it keeps only a factory for the
        # VeraAgent it hands off to once a human answers (see transfer_to_verification).
        self._make_verification_agent = functools.partial(
            VeraAgent,
            boundary,
            session_id,
            instructions=verification_instructions,
            greeting=verification_greeting,
        )
        self._turns = 0  # IVR turns taken; the give-up backstop caps this
        # Patient end-of-turn detection for the IVR phase (waits for the machine to finish before
        # answering); a per-agent override that reverts to the snappy human default at the handoff.
        super().__init__(
            instructions=build_ivr_instructions(),
            tools=[],
            turn_handling=ivr_turn_handling(),
        )

    def _end_navigation(self, reason: str) -> None:
        """Hang up the call cleanly (drain pending audio), to bail out of an unresolvable IVR
        loop rather than thrash forever. Mirrors VeraAgent's end_call."""
        logger.warning("IVR navigator: ending call — %s", reason)
        self.session.shutdown(drain=True)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        # Deterministic backstop so the call can never loop forever even if the model never calls
        # give_up. Scoped to the navigator: the counter is gone once the handoff swaps in VeraAgent.
        self._turns += 1
        if self._turns > _IVR_MAX_TURNS:
            self._end_navigation(f"turn cap reached ({_IVR_MAX_TURNS} IVR turns, no human)")
            raise StopResponse

    @function_tool
    async def give_up(self) -> str:
        """Give up and end the call. Call this ONLY after the full escalation ladder
        (rep_keyword → press 0 → "Agent") has been tried and the SAME menu keeps looping with no
        progress — a self-service menu that never routes to a human. Ends the call cleanly."""
        self._end_navigation("gave up on an unresolvable IVR loop")
        return "Ending the call."

    @function_tool
    async def transfer_to_verification(self) -> Agent:
        """Hand the call to the verification agent. Call this ONLY when a live human
        representative has clearly greeted you — a personal name paired with an open request
        for your info (e.g. "Hi, this is Martha, who am I speaking with?")."""
        logger.info("handoff: IVR navigator -> verification agent")
        return self._make_verification_agent()

    @function_tool
    async def press_keypad(self, digits: str) -> str:
        """Press keypad digits on the phone menu (sends DTMF tones). Use ONLY for digits
        the IVR actually offered (e.g. "press 1 for eligibility"); never invent an account,
        member, or ID number. `digits` may contain 0-9, * or #."""
        # Log the count only — a DTMF sequence can be a member ID/NPI (PHI), and the
        # return string feeds the LLM/traces, so neither echoes the raw digits.
        count = len(digits.strip())
        try:
            await send_dtmf(get_job_context().room.local_participant, digits)
        except InvalidDtmfError as exc:
            return f"Could not send those keys: {exc}"
        except DtmfTransportError:
            # Surface the failure (the logged exception carries the real cause) instead of
            # letting the tool runner swallow it — the historical reason a failed press
            # looked like "nothing happened" on the line.
            logger.exception("press_keypad: DTMF publish failed (%d tone(s))", count)
            return "Could not send the keypad tones over the call; continue without pressing."
        logger.info("press_keypad: sent %d DTMF tone(s)", count)
        return "Sent the keypad tones."
