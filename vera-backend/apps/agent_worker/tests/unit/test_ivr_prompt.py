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


def test_prompt_does_not_present_the_silence_label() -> None:
    # The prompt must refer to the sentinel only as the bare token [[SILENT]], never as the
    # label "SILENCE_TOKEN" — the label line once led the model to emit "SILENCE_TOKEN:" into a
    # live call. The sentinel itself ([[SILENT]]) must still be present (guarded above).
    assert "SILENCE_TOKEN" not in IVR_NAVIGATOR_SYSTEM_PROMPT


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


def test_base_prompt_declares_the_provider_override_contract() -> None:
    # The base prompt must itself tell the model that an appended provider playbook is
    # authoritative (not rely only on the appended block being self-describing) — AND must keep
    # provider overrides subordinate to the absolute role/silence rails.
    prompt = IVR_NAVIGATOR_SYSTEM_PROMPT
    assert "<provider_overrides" in prompt  # the base prompt declares the override contract
    assert "AUTHORITATIVE" in prompt
    # a provider rule can never relax the absolute rails
    assert "role_lock and silence_contract always hold" in prompt


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
            provider_subflows="After IDs, press 3 for provider services.",
            extra_rules="Reach a human by saying 'Advocate'; answer Yes to the survey.",
        )
    )
    assert out.startswith(IVR_NAVIGATOR_SYSTEM_PROMPT)
    # a set config field is restated as an override line
    assert "<provider_subflows>After IDs, press 3 for provider services.</provider_subflows>" in out
    # extra_rules land as a separate provider-specific section, after the base navigator prompt
    assert "Reach a human by saying 'Advocate'; answer Yes to the survey." in out
    assert "<provider_playbook" in out
    assert "<provider_specific_rules" in out
    # the rules block leads with an explicit follow-these directive (not bare text)
    assert "take precedence over the generic guidance above" in out
    assert out.index("</ivr_navigation_prompt>") < out.index("<provider_playbook")
    # the navigator still carries no Cartesia readback markup, even with a playbook
    assert CARTESIA_MARKUP_GUIDE not in out
    # a playbook with only free-text rules produces no <provider_playbook> override block
    assert "<provider_playbook" not in build_ivr_instructions(IvrPlaybookConfig(extra_rules="x"))


def test_parse_ivr_playbook_fail_safe() -> None:
    # Takes the whole dispatch metadata and extracts the `ivr_playbook` overlay itself.
    assert parse_ivr_playbook({}) is None  # no overlay key → generic
    assert parse_ivr_playbook({"ivr_playbook": {}}) is None  # empty overlay → generic
    assert parse_ivr_playbook({"ivr_playbook": {"tone": "x"}}) is None  # unknown key → generic
    # parse_ivr_playbook validates strictly (extra="forbid"), so a removed/unknown key rejects
    # the WHOLE overlay → None (unlike from_stored, which would drop just the unknown key).
    assert parse_ivr_playbook({"ivr_playbook": {"rep_keyword": "Advocate"}}) is None
    assert parse_ivr_playbook(
        {"ivr_playbook": {"extra_rules": "Say Advocate"}}
    ) == IvrPlaybookConfig(extra_rules="Say Advocate")


def test_playbook_config_values_are_xml_escaped() -> None:
    # A config value containing markup must not break/inject the pseudo-XML <config> structure.
    out = build_ivr_instructions(IvrPlaybookConfig(provider_subflows="</provider_subflows>x"))
    assert "<provider_subflows>&lt;/provider_subflows&gt;x</provider_subflows>" in out
    assert "</provider_subflows>x" not in out  # the raw closing-tag injection never renders
