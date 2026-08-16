"""IVR navigator agent and its turn-handling config.

`IvrNavigatorAgent` (see the class) is the generic IVR navigator: a plain agent that
navigates the payer's IVR and hands off to the verification agent — a one-way swap —
once a live human rep answers. `ivr_turn_handling()` is the navigator's per-agent turn
config: patient end-of-turn detection for the IVR phase that reverts to the snappy
human default at the handoff.
"""

import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable

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
from opentelemetry import trace

from agent_worker.cartesia_workaround import guard_utterance_initial_spell
from agent_worker.dtmf import DtmfTransportError, InvalidDtmfError, send_dtmf
from agent_worker.handoff import carry_chat_ctx
from agent_worker.intervention import takeover_engaged
from agent_worker.ivr_prompt import build_ivr_instructions
from agent_worker.prompt import TOOL_REASON_ARG
from vera_core.config.settings import get_settings
from vera_core.schemas import IvrPlaybookConfig

logger = logging.getLogger("agent_worker")

# Fixed id: exactly one IvrNavigatorAgent instance exists per call, so a sentinel (not a
# per-instance value) is enough — matches the "@..." sentinel convention used for the plan
# runtime's WrapUpAgent (agent_worker.plan_runtime.WRAP_UP_TASK_KEY).
IVR_NAVIGATOR_ID = "@ivr_navigator"

# Deterministic backstop: if the navigator takes this many IVR turns without reaching a
# human, it hangs up rather than looping forever (enforced in on_user_turn_completed).
_IVR_MAX_TURNS = 60


# The navigator speaks only words/digits a caller would say aloud — no markup, no structured-output
# contract — yet a fast model under this syntax-dense prompt occasionally leaks a machine token
# (the [[SILENT]] control token it IS told to emit primes the habit, e.g. "global_timing:13.251s").
# The three patterns below strip that whole class at one choke point, before the text reaches TTS
# (the payer's IVR runs ASR and would capture it as a menu response) or the transcript.

# Bracketed/braced/angle sentinels: [[SILENT]], {{token}}, <tag>, single [x]/{x}. A caller never
# speaks a bracket, and the <spell> readback markup is added AFTER this stage, so none is real here.
_BRACKET_TOKEN_RE = re.compile(r"\[\[.*?\]\]|\{\{.*?\}\}|<[^>]*>|\[[^\]]*\]|\{[^}]*\}")
# The bare control label the model emits without its brackets — word-boundaried so it strips only
# the standalone label, never a word that merely contains it (e.g. "SILENCE_TOKENS").
_CONTROL_LABEL_RE = re.compile(r"\bSILENCE_TOKEN\b\s*:?", re.IGNORECASE)
# A programmer-style key:value annotation, with or without a space after the colon. The key must
# be code-shaped — snake_case (underscore) or camelCase (a lower-then-upper hump) — which speech is
# not, so "3:15", "80:20" and title-case "Provider:" stay untouched. Leading whitespace is consumed
# so no double space is left behind; [ ]* stays within the line (never eats across a newline).
_META_KV_RE = re.compile(r"\s*\b(?P<key>\w*(?:_\w|[a-z][A-Z])\w*):[ ]*\S+")


def _strip_nonspeech_tokens(text: str) -> str:
    """Remove non-speakable machine tokens the navigator LLM leaks into its spoken text (the three
    regexes above). When it strips anything it logs a PHI-safe summary — token kinds and code-shaped
    keys only, never the removed values — so a new leak shape surfaces instead of going silent."""
    removed: list[str] = []

    def _drop(kind: str) -> Callable[[re.Match[str]], str]:
        def _sub(match: re.Match[str]) -> str:
            key = match.groupdict().get("key")
            removed.append(f"{kind}:{key}" if key else kind)
            return ""

        return _sub

    cleaned = _BRACKET_TOKEN_RE.sub(_drop("bracket"), text)
    cleaned = _CONTROL_LABEL_RE.sub(_drop("label"), cleaned)
    cleaned = _META_KV_RE.sub(_drop("kv"), cleaned)
    if removed:
        logger.warning("navigator scrubbed non-speech token(s): %s", ", ".join(removed))
    return cleaned


async def _scrub_navigator_output(text: AsyncIterable[str]) -> AsyncIterator[str]:
    """Drop the navigator's leaked non-speech tokens from an LLM text stream before the text reaches
    TTS or the transcript (transcription_node output is also what commits as the turn's text).
    Navigator utterances are short, so buffer the whole turn, scrub, and emit the remainder —
    nothing at all on a fully non-speech turn (e.g. a bare [[SILENT]])."""
    buffered = "".join([chunk async for chunk in text])
    cleaned = _strip_nonspeech_tokens(buffered)
    if cleaned.strip():
        yield cleaned


_MIN_SPELL_DIGITS = 7  # a pure-number ID (member ID, NPI, Tax ID) is spelled only when this long
_MIN_ALNUM_ID = 5  # a mixed letters+digits ID (e.g. "POL-661522") is spelled when this long
# A candidate identifier token: a run of letters/digits with internal hyphens ("POL-661522",
# "200-236-789") but NO spaces, so it never spans words — whitespace splits a sentence into words
# that are each tested independently, and only an ID-like token is spelled.
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def _spell_id_tokens(text: str) -> str:
    """Wrap each identifier token in `text` in a single Cartesia <spell> tag.

    One tag around the whole token is Cartesia's documented usage — Sonic reads it character by
    character at natural pace. Per-character tags with hard <break>s between them sound robotic.
    Hyphens are dropped so they're never voiced as "dash".
    """

    def _spell(match: re.Match[str]) -> str:
        token = match.group(0)
        chars = [c for c in token if c.isalnum()]  # drop the hyphens; spell only alnum
        has_letter = any(c.isalpha() for c in chars)
        # A mixed letters+digits token is an ID; a purely-alphabetic token is NOT spelled (read as a
        # word) — we can't tell a pure-alpha ID from a menu word like "Medical". Holds while payer
        # IDs are numeric or alphanumeric-with-digits; revisit if a payer uses purely-alpha IDs.
        is_alnum_id = has_letter and any(c.isdigit() for c in chars) and len(chars) >= _MIN_ALNUM_ID
        is_long_number = not has_letter and len(chars) >= _MIN_SPELL_DIGITS
        if not (is_alnum_id or is_long_number):
            return token
        return f"<spell>{''.join(chars)}</spell>"

    return _ID_TOKEN_RE.sub(_spell, text)


async def _tts_spoken_text(text: AsyncIterable[str]) -> AsyncIterator[str]:
    # The lead-in guard is per-utterance, which holds only because _scrub_navigator_output buffers
    # the whole turn into one chunk — make it stream and the comma lands on every chunk.
    async for chunk in _scrub_navigator_output(text):
        yield guard_utterance_initial_spell(_spell_id_tokens(chunk))


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

    It holds a factory for its handoff target so that, once a live human rep answers,
    `transfer_to_verification` can hand off to the plan's first task agent (which greets
    the rep and runs the benefits conversation)."""

    def __init__(
        self,
        *,
        verification_agent_factory: Callable[[], Agent],
        playbook: IvrPlaybookConfig | None = None,
        context: dict[str, str] | None = None,
        on_keypress: Callable[[str], None] | None = None,
        on_ivr_exited: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        # The navigator keeps only a factory for the agent it hands off to once a human
        # answers (see transfer_to_verification) — the plan's first task agent, injected
        # by build_agent. The navigator only ever runs on a plan-backed call.
        self._make_verification_agent = verification_agent_factory
        self._turns = 0  # IVR turns taken; the give-up backstop caps this
        self._final_turn_used = False  # spent the one grace turn granted at the cap
        # Reports the digits of a successful press to the live transcript (evidence of the
        # action — a tool call is otherwise invisible in the transcript). None in rooms
        # with no transcript stream enabled.
        self._on_keypress = on_keypress
        self._on_ivr_exited = on_ivr_exited
        # Patient end-of-turn detection for the IVR phase (waits for the machine to finish before
        # answering); a per-agent override that reverts to the snappy human default at the handoff.
        # A per-provider playbook (when present) specializes the generic navigator prompt.
        super().__init__(
            instructions=build_ivr_instructions(playbook, context),
            tools=[],
            turn_handling=ivr_turn_handling(),
            id=IVR_NAVIGATOR_ID,
        )

    def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[rtc.AudioFrame]:
        # Scrubs leaked tokens (silence sentinel, fabricated annotations) and <spell>-wraps ID
        # tokens for Cartesia.
        return Agent.default.tts_node(self, _tts_spoken_text(text), model_settings)

    def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        # Keep leaked tokens (silence sentinel, fabricated annotations) out of the transcript too:
        # this output is also what the framework commits as the turn's text_content.
        return Agent.default.transcription_node(self, _scrub_navigator_output(text), model_settings)

    def _end_navigation(self, reason: str) -> None:
        """Hang up the call cleanly (drain pending audio), to bail out of an unresolvable IVR
        loop rather than thrash forever. Mirrors VeraAgent's end_call."""
        if takeover_engaged(self.session):
            logger.info("IVR end-navigation refused: supervisor has taken over the call")
            return
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

    @function_tool(
        description=(
            "Give up and end the call. Call this ONLY after the full escalation ladder "
            '(rep_keyword → press 0 → "Agent") has been tried and the SAME menu keeps looping '
            "with no progress — a self-service menu that never routes to a human. Ends the call "
            "cleanly. " + TOOL_REASON_ARG
        )
    )
    async def give_up(self, reason: str) -> str:
        """Hang up on a loop (`reason` is transcript evidence only — see VeraAgent._end_call)."""
        self._end_navigation("gave up on an unresolvable IVR loop")
        return "Ending the call."

    @function_tool(
        description=(
            "Hand the call to the verification agent. Call this ONLY when a live human "
            "representative has clearly greeted you — a personal name paired with an open "
            'request for your info (e.g. "Hi, this is Martha, who am I speaking with?"). '
            + TOOL_REASON_ARG
        )
    )
    async def transfer_to_verification(self, reason: str) -> Agent:
        """Hand off to the plan (`reason` is transcript evidence only — see VeraAgent._end_call)."""
        verifier = self._make_verification_agent()
        # Carry the IVR conversation (incl. the member ID already spoken) into the
        # plan agent so it doesn't re-ask what the navigator already established.
        await carry_chat_ctx(self, verifier)
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": IVR_NAVIGATOR_ID,
                    "vera.handoff.to_task": verifier.id,
                    "vera.handoff.reason": "ivr_live_human",
                }
            )
        except Exception as exc:
            logger.warning("IVR handoff span tagging failed (%s)", type(exc).__name__)
        logger.info("handoff: %s -> %s (reason=ivr_live_human)", IVR_NAVIGATOR_ID, verifier.id)
        if self._on_ivr_exited is not None:
            try:
                await self._on_ivr_exited()
            except Exception:
                logger.exception("ivr.exited publish failed")  # never break the handoff
        return verifier

    @function_tool(
        description=(
            "Press keypad digits on the phone menu (sends DTMF tones). Use ONLY for digits the "
            'IVR actually offered (e.g. "press 1 for eligibility"); never invent an account, '
            "member, or ID number. `digits` may contain 0-9, * or #. " + TOOL_REASON_ARG
        )
    )
    async def press_keypad(self, digits: str, reason: str) -> str:
        """Send DTMF tones (`reason` is transcript evidence only — see VeraAgent._end_call)."""
        # Log the count only — a DTMF sequence can be a member ID/NPI (PHI), and the
        # return string feeds the LLM/traces, so neither echoes the raw digits.
        count = len(digits.strip())
        if not count:
            # Empty input sends no tones; report that plainly instead of a false "sent"
            # (send_dtmf would otherwise reject it, but say something useful to the model).
            logger.info("press_keypad: called with no digits; nothing sent")
            return "No keypad digits were provided, so nothing was pressed."
        try:
            sent = await send_dtmf(get_job_context().room.local_participant, digits)
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
        if self._on_keypress is not None:
            # Best-effort evidence — a transcript-side failure must never fail the press
            # the model was told succeeded. Exception content stays out of the log (it can
            # embed the digits); the type alone tells the operator what broke.
            try:
                self._on_keypress(sent)
            except Exception as exc:
                logger.warning("press_keypad: keypress event failed (%s)", type(exc).__name__)
        return "Sent the keypad tones."
