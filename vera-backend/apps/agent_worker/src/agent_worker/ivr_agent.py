"""IVR navigator agent and its turn-handling config.

`IvrNavigatorAgent` (see the class) is the generic IVR navigator: a plain agent with no
PHI-wall node overrides that navigates the payer's IVR and hands off to `VeraAgent` — a
one-way swap — once a live human rep answers (from then on the PHI-wall overrides apply).
`ivr_turn_handling()` is the navigator's per-agent turn config: patient end-of-turn detection
for the IVR phase that reverts to the snappy human default at the handoff.
"""

import functools
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator

from livekit import rtc
from livekit.agents import (
    Agent,
    ModelSettings,
    StopResponse,
    TurnHandlingOptions,
    function_tool,
    get_job_context,
    llm,
)

from agent_worker.dtmf import DtmfTransportError, InvalidDtmfError, send_dtmf
from agent_worker.ivr_prompt import SILENCE_TOKEN, build_ivr_instructions
from vera_core.config.settings import get_settings
from vera_core.phi import PHIBoundaryProtocol
from vera_core.schemas import IvrPlaybookConfig

logger = logging.getLogger("agent_worker")

# Deterministic backstop: if the navigator takes this many IVR turns without reaching a
# human, it hangs up rather than looping forever (enforced in on_user_turn_completed).
_IVR_MAX_TURNS = 60


# Matches the silence sentinel ([[SILENT]]) AND the label the model sometimes emits by mistake
# ("SILENCE_TOKEN:", from the prompt's silence contract) — case-insensitive, tolerant of a stray
# colon/whitespace. The label alternative is word-boundaried so it can only strip the standalone
# label, never splice a word that merely contains it (e.g. "SILENCE_TOKENS"). Stripping both keeps
# a "stay silent" turn from reaching TTS when the model's rendering of the sentinel drifts.
_SILENCE_RE = re.compile(rf"{re.escape(SILENCE_TOKEN)}|\bSILENCE_TOKEN\b\s*:?", re.IGNORECASE)


async def _strip_silence_token(text: AsyncIterable[str]) -> AsyncIterator[str]:
    """Drop the navigator's silence sentinel from an LLM text stream.

    The prompt tells the model to emit exactly ``[[SILENT]]`` when the right action is to stay
    quiet — the common case. Without this, the default tts_node would synthesize that token (or
    the near-miss ``SILENCE_TOKEN:`` the model sometimes emits instead) into the live call.
    Navigator utterances are short, so buffer the whole turn, strip the sentinel/label, and emit
    the remainder — nothing at all on a silent turn.
    """
    buffered = "".join([chunk async for chunk in text])
    cleaned = _SILENCE_RE.sub("", buffered)
    if cleaned.strip():
        yield cleaned


def ivr_turn_handling() -> TurnHandlingOptions:
    """Fresh `turn_handling` for the IVR navigator (pass as `Agent(turn_handling=...)`).

    Tuned patient for a machine, not a person:
    - `turn_detection="vad"`, NOT the human-trained EnglishModel — an IVR reads menus and
      readouts at machine cadence, so plain VAD end-of-turn fits and stays fully local.
    - preemptive generation OFF — keeps a tiny output buffer so a false-interruption pause
      can't discard the start of an utterance (SIP self-echo clip: "Medical" -> "dical").
    - the endpointing delays are the key IVR-patience tunable; they live in settings so they
      can be adjusted without a code change (see `Settings.ivr_endpointing_*`).
    """
    settings = get_settings()
    return {
        "endpointing": {
            "min_delay": settings.ivr_endpointing_min_delay,
            "max_delay": settings.ivr_endpointing_max_delay,
        },
        "preemptive_generation": {"enabled": False},
        "turn_detection": "vad",
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
        playbook: IvrPlaybookConfig | None = None,
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
        self._final_turn_used = False  # spent the one grace turn granted at the cap
        # Patient end-of-turn detection for the IVR phase (waits for the machine to finish before
        # answering); a per-agent override that reverts to the snappy human default at the handoff.
        # A per-provider playbook (when present) specializes the generic navigator prompt.
        super().__init__(
            instructions=build_ivr_instructions(playbook),
            tools=[],
            turn_handling=ivr_turn_handling(),
        )

    def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[rtc.AudioFrame]:
        # Not a PHI seam (the navigator injects no call_data, so there's nothing to hydrate);
        # this only strips the silence sentinel so a "stay silent" turn makes no sound.
        return Agent.default.tts_node(self, _strip_silence_token(text), model_settings)

    def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        # Keep the sentinel out of the forwarded transcript too, for the same reason.
        return Agent.default.transcription_node(self, _strip_silence_token(text), model_settings)

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
        if self._turns <= _IVR_MAX_TURNS:
            return
        # Over the cap. Grant exactly one grace turn before hanging up: if this incoming turn
        # is a live rep finally answering, letting it generate lets the model recognize the
        # human and call transfer_to_verification (which swaps this agent out) instead of being
        # dropped. Only if we're still navigating the turn after that do we hard-stop — a genuine
        # loop the model can't escape. (A turn counter can't tell a human from the IVR, so one
        # preempted turn is unavoidable; this moves it past the model's last chance to hand off.)
        if not self._final_turn_used:
            self._final_turn_used = True
            return
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
        if not count:
            # Empty input sends no tones; report that plainly instead of a false "sent"
            # (send_dtmf would otherwise reject it, but say something useful to the model).
            logger.info("press_keypad: called with no digits; nothing sent")
            return "No keypad digits were provided, so nothing was pressed."
        try:
            await send_dtmf(get_job_context().room.local_participant, digits)
        except InvalidDtmfError:
            # The exception names the offending characters; keep them out of the return
            # (they feed the LLM/traces and can be PHI). A fixed message says enough.
            logger.info("press_keypad: rejected unsupported keypad input (%d char(s))", count)
            return "Those keys aren't all valid keypad digits (use only 0-9, * or #); nothing sent."
        except DtmfTransportError:
            # Surface the failure (the logged exception carries the real cause) instead of
            # letting the tool runner swallow it — the historical reason a failed press
            # looked like "nothing happened" on the line.
            logger.exception("press_keypad: DTMF publish failed (%d tone(s))", count)
            return "Could not send the keypad tones over the call; continue without pressing."
        logger.info("press_keypad: sent %d DTMF tone(s)", count)
        return "Sent the keypad tones."
