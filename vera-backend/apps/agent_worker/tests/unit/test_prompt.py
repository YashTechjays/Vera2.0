import logging

import pytest

from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    VOICE_LAB_GREETING,
    build_voice_lab_instructions,
    parse_persona_tweak,
    resolve_voice_lab_greeting,
)
from vera_core.schemas import PersonaTweak


def test_cartesia_guide_survives_plan_only() -> None:
    # The real-call monolithic SYSTEM_PROMPT is gone (plan-only), but the TTS markup
    # guide stays — plan_runtime appends it so CPT codes keep <spell> wrapping.
    assert "<spell>" in CARTESIA_MARKUP_GUIDE


def test_no_prompt_text_spells_icd_with_digits() -> None:
    """ "ICD-10" is voiced "I-C-D one zero" — heard on a live call. Every prompt string the
    agent copies into speech says "ICD ten"; the space rather than a hyphen keeps any TTS
    provider from reading the separator aloud as "dash". The Excel export column is
    deliberately still "ICD-10" — that one is read, not spoken."""
    import agent_worker.prompt as prompt_module

    spoken = [
        value
        for name, value in vars(prompt_module).items()
        if isinstance(value, str) and not name.startswith("__")
    ]
    assert spoken, "no prompt constants found — did the module move?"
    for text in spoken:
        assert "ICD-10" not in text
        assert "ICD-Ten" not in text


def test_voice_lab_instructions_carry_persona_and_markup_guide() -> None:
    instructions = build_voice_lab_instructions()
    assert "infertility" in instructions.lower()  # the preview persona
    assert "<spell>" in instructions  # markup guide appended


def test_voice_lab_instructions_append_tenant_extra() -> None:
    instructions = build_voice_lab_instructions(PersonaTweak(extra_instructions="Be extra warm."))
    assert "Be extra warm." in instructions


def test_resolve_voice_lab_greeting_default_and_override() -> None:
    assert resolve_voice_lab_greeting() == VOICE_LAB_GREETING
    assert resolve_voice_lab_greeting(PersonaTweak(greeting="Custom.")) == "Custom."


def test_parse_persona_tweak_fail_safe() -> None:
    assert parse_persona_tweak(None) == PersonaTweak()
    assert parse_persona_tweak("") == PersonaTweak()
    assert parse_persona_tweak("not json") == PersonaTweak()
    assert parse_persona_tweak("[1, 2]") == PersonaTweak()  # non-dict metadata
    # unknown key inside the nested tweak
    assert parse_persona_tweak('{"persona_tweak": {"tone": "formal"}}') == PersonaTweak()
    assert parse_persona_tweak('{"persona_tweak": {"greeting": "Hi"}}') == PersonaTweak(
        greeting="Hi"
    )


def test_parse_persona_tweak_ignores_sibling_dispatch_keys() -> None:
    # IVR-enabled dispatches carry sibling keys; the nested tweak must survive them.
    metadata = (
        '{"persona_tweak": {"greeting": "Hi"}, "enable_ivr_navigation": true,'
        ' "ivr_playbook": {"extra_rules": "Say Advocate to reach a human."}}'
    )
    assert parse_persona_tweak(metadata) == PersonaTweak(greeting="Hi")
    # no nested key (e.g. Voice Lab dispatch) → no-op tweak, not an error
    assert parse_persona_tweak('{"wait_for_speaker": true}') == PersonaTweak()


def test_parse_persona_tweak_accepts_legacy_flat_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Deploy skew: a not-yet-updated control plane sends the flat shape (the whole dict IS
    # the tweak). Accept it for one release and warn, so the persona isn't silently dropped.
    with caplog.at_level(logging.WARNING, logger="agent_worker"):
        assert parse_persona_tweak('{"greeting": "Hi"}') == PersonaTweak(greeting="Hi")
    assert any("legacy flat metadata shape" in r.message for r in caplog.records)
