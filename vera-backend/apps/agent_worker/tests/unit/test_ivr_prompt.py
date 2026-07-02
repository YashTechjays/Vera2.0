"""Tests for the generic IVR-navigator prompt (agent_worker.ivr_prompt)."""

from agent_worker.ivr_prompt import IVR_NAVIGATOR_SYSTEM_PROMPT, build_ivr_instructions
from agent_worker.prompt import CARTESIA_MARKUP_GUIDE


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
