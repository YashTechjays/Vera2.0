"""Tests for the generic IVR-navigator prompt (agent_worker.ivr_prompt)."""

from agent_worker.ivr_prompt import (
    IVR_NAVIGATOR_SYSTEM_PROMPT,
    SILENCE_TOKEN,
    build_ivr_instructions,
    parse_ivr_playbook,
)
from agent_worker.prompt import CARTESIA_MARKUP_GUIDE
from vera_core.schemas import IvrPlaybookConfig


def test_silence_token_matches_the_prompt_sentinel() -> None:
    # The tts/transcription filter strips SILENCE_TOKEN; if the prompt's literal sentinel
    # ever drifts from the constant, the token would be spoken aloud. Guard against that.
    assert SILENCE_TOKEN in IVR_NAVIGATOR_SYSTEM_PROMPT


def test_ivr_navigator_prompt_is_generic_and_cascade_compatible() -> None:
    prompt = IVR_NAVIGATOR_SYSTEM_PROMPT
    lower = prompt.lower()
    # generic eligibility/benefits navigator — not the infertility persona
    assert "eligibility" in lower
    assert "infertility" not in lower
    # drives the call toward a human representative
    assert "representative" in lower
    # cascade-compatible: plain spoken words, NOT the structured-action contract
    assert "responseschema" not in lower
    assert '"action"' not in prompt  # no JSON action object defined
    # XML-structured, reactive navigator: two-mode state machine + per-provider response rules
    assert "<ivr_navigation_prompt>" in prompt
    assert "<response_rules>" in prompt
    assert "announcement mode" in lower
    assert "<prompt_mode>" in prompt
    assert "silent" in lower  # the core reactive discipline
    # the model is told to send DTMF by calling the press_keypad tool (not by speaking digits)
    assert "press_keypad" in lower
    # and to hand off to the verification agent (not speak an opener) once a human answers
    assert "transfer_to_verification" in lower


def test_build_ivr_instructions_omits_the_cartesia_guide() -> None:
    # Unlike the chat persona, the navigator drives TTS with plain words and needs no
    # Cartesia readback markup — the instructions are just the navigator prompt.
    combined = build_ivr_instructions()
    assert combined == IVR_NAVIGATOR_SYSTEM_PROMPT
    assert CARTESIA_MARKUP_GUIDE not in combined


def test_empty_playbook_is_no_op() -> None:
    # No playbook, None, and an all-defaults playbook all yield the generic navigator.
    assert build_ivr_instructions(None) == build_ivr_instructions()
    assert build_ivr_instructions(IvrPlaybookConfig()) == build_ivr_instructions()


def test_playbook_overrides_and_rules_appended_after_base_prompt() -> None:
    out = build_ivr_instructions(
        IvrPlaybookConfig(
            rep_keyword="Advocate",
            survey_answer="Yes",
            extra_rules="After IDs, press 3 for provider services.",
        )
    )
    assert out.startswith(IVR_NAVIGATOR_SYSTEM_PROMPT)
    # set knobs are restated as overrides; unset knobs are omitted
    assert "<rep_keyword>Advocate</rep_keyword>" in out
    assert "<survey_answer>Yes</survey_answer>" in out
    assert "<date_scope>" not in out.split("</ivr_navigation_prompt>")[1]
    # extra_rules land as a provider-specific section, after the base navigator prompt
    assert "After IDs, press 3 for provider services." in out
    assert "<provider_playbook" in out
    assert "<provider_specific_rules" in out
    assert out.index("</ivr_navigation_prompt>") < out.index("<provider_playbook")
    # the navigator still carries no Cartesia readback markup, even with a playbook
    assert CARTESIA_MARKUP_GUIDE not in out


def test_parse_ivr_playbook_fail_safe() -> None:
    assert parse_ivr_playbook(None) is None
    assert parse_ivr_playbook({}) is None  # empty overlay → generic
    assert parse_ivr_playbook({"tone": "formal"}) is None  # unknown key rejected → generic
    assert parse_ivr_playbook({"rep_keyword": "Advocate"}) == IvrPlaybookConfig(
        rep_keyword="Advocate"
    )
