"""Tests for the generic IVR-navigator prompt (agent_worker.ivr_prompt)."""

import re

from agent_worker.ivr_prompt import (
    IVR_NAVIGATOR_SYSTEM_PROMPT,
    SILENCE_TOKEN,
    build_ivr_instructions,
    parse_agent_context,
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
    assert "<answers>" in prompt
    # <modes>/<prompt_mode> folded into the audio classifier; the behavior they pinned lives on
    assert "<what_you_hear>" in prompt
    assert "announcement" in lower  # calls start there and leave on the first real prompt
    assert "silent" in lower  # the core reactive discipline
    # the model is told to send DTMF by calling the press_keypad tool (not by speaking digits)
    assert "press_keypad" in lower
    # and to hand off to the verification agent (not speak an opener) once a human answers
    assert "transfer_to_verification" in lower


def test_base_prompt_declares_the_provider_override_contract() -> None:
    # The base prompt must itself tell the model that an appended provider playbook is
    # authoritative (not rely only on the appended block being self-describing) — AND must keep
    # provider overrides subordinate to the absolute identity/output rails.
    prompt = IVR_NAVIGATOR_SYSTEM_PROMPT
    assert "provider_playbook / provider_specific_rules overrides" in prompt
    # a provider rule can never relax the absolute rails, which the header names by section
    assert "never identity or output_form" in prompt
    assert "<identity>" in prompt and "<output_form>" in prompt


def test_build_ivr_instructions_resolves_placeholders_and_omits_cartesia_guide() -> None:
    # Unlike the chat persona, the navigator drives TTS with plain words and needs no
    # Cartesia readback markup. With no context, every {{token}} collapses to empty — no raw
    # placeholder ever reaches the model.
    combined = build_ivr_instructions()
    assert "{{" not in combined
    assert CARTESIA_MARKUP_GUIDE not in combined


def test_empty_playbook_is_no_op() -> None:
    # No playbook, None, and an all-defaults playbook all yield the generic navigator.
    assert build_ivr_instructions(None) == build_ivr_instructions()
    assert build_ivr_instructions(IvrPlaybookConfig()) == build_ivr_instructions()


def test_context_fills_identifier_placeholders() -> None:
    out = build_ivr_instructions(
        context={
            "patient_name": "jane roe",
            "member_id": "ZZZ123",
            "patient_dob": "03/07/1990",
            "doctor_npi": "9998887776",
            "hospital_tax_id": "112223333",
        }
    )
    assert "{{" not in out  # no placeholder leaks
    for value in ("ZZZ123", "03/07/1990", "jane roe", "9998887776", "112223333"):
        assert value in out
    # the read-back rule now carries the real name to verify against
    assert "matches jane roe" in out
    # supplying real values displaces the synthetic defaults
    assert "200236789" not in out


def test_context_missing_tokens_resolve_to_empty() -> None:
    # Only member_id known → the other placeholders collapse to empty (no default value, no raw
    # {{token}}); the known one is still filled.
    out = build_ivr_instructions(context={"member_id": "M1"})
    assert "{{" not in out
    assert "M1" in out


def test_playbook_overrides_and_rules_appended_after_base_prompt() -> None:
    out = build_ivr_instructions(
        IvrPlaybookConfig(
            provider_subflows="After IDs, press 3 for provider services.",
            extra_rules="Reach a human by saying 'Advocate'; answer Yes to the survey.",
        )
    )
    # the base (token-substituted) navigator prompt is the prefix; the overlay is appended
    assert out.startswith(build_ivr_instructions())
    # a set config field is restated as an override line
    assert 'provider_subflows="After IDs, press 3 for provider services."' in out
    # extra_rules land as a separate provider-specific section, after the base navigator prompt
    assert "Reach a human by saying 'Advocate'; answer Yes to the survey." in out
    assert "<provider_playbook" in out
    assert "<provider_specific_rules" in out
    # the overlay's own guard must name sections the base prompt actually has, or it constrains
    # nothing (it named the pre-reorg role_lock / silence_contract for one release)
    for rail in ("identity", "output_form"):
        assert f"<{rail}>" in out and rail in out.split("<provider_specific_rules>")[1]
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


def test_parse_agent_context_fail_safe() -> None:
    # Takes the whole dispatch metadata and extracts the `agent_context` map itself.
    assert parse_agent_context({}) is None  # no key → navigator uses default tokens
    assert parse_agent_context({"agent_context": {}}) is None  # empty → defaults
    assert parse_agent_context({"agent_context": "nope"}) is None  # wrong type → defaults
    # non-str values are dropped; an all-dropped map yields None
    assert parse_agent_context({"agent_context": {"member_id": 5}}) is None
    assert parse_agent_context({"agent_context": {"member_id": "M1", "x": 5}}) == {
        "member_id": "M1"
    }


def test_playbook_config_values_are_xml_escaped() -> None:
    # A config value containing markup must not break/inject the pseudo-XML <config> structure.
    out = build_ivr_instructions(IvrPlaybookConfig(provider_subflows='</config>" x'))
    # both the markup and the quote are escaped, so neither can close the attribute or the block
    assert 'provider_subflows="&lt;/config&gt;&quot; x"' in out
    assert "</provider_subflows>x" not in out  # the raw closing-tag injection never renders


def test_config_keys_match_the_playbook_schema() -> None:
    # _PLAYBOOK_CONFIG_KEYS is derived from IvrPlaybookConfig so a new field is emitted without
    # touching ivr_prompt.py — but that only works while the prompt's <config> keys use the SAME
    # names. A rename on either side silently renders an override the rules never read.
    config = IVR_NAVIGATOR_SYSTEM_PROMPT.split("<config", 1)[1].split("/>", 1)[0]
    prompt_keys = set(re.findall(r"(\w+)=", config))
    for field in IvrPlaybookConfig.model_fields:
        if field == "extra_rules":  # free text, rendered as its own section, not a <config> key
            continue
        assert field in prompt_keys, f"schema field {field!r} has no matching <config> key"
    # every prompt key is either schema-backed or a documented not-yet-backed knob
    unbacked = {
        "rep_keyword",
        "multiple_patients_answer",
        "survey_answer",
        "date_scope",
        "callback_vs_hold",
        "transition_trigger",
    }
    assert prompt_keys - set(IvrPlaybookConfig.model_fields) == unbacked
