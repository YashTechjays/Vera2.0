import logging

import pytest

from agent_worker.prompt import (
    BASE_PERSONA,
    CARTESIA_MARKUP_GUIDE,
    GREETING,
    build_instructions,
    parse_persona_tweak,
    resolve_greeting,
)
from vera_core.schemas import PersonaTweak


def test_fallback_persona_is_generic_and_includes_cartesia_guide() -> None:
    # The fallback persona is schema-agnostic behaviour only (the verification
    # content now comes from the compiled call plan, not this constant).
    assert "PERSONA" in BASE_PERSONA
    assert "end_call" in BASE_PERSONA
    assert GREETING.startswith("Hi, I'm calling on behalf of a patient")
    combined = build_instructions()
    assert combined.startswith(BASE_PERSONA)
    assert CARTESIA_MARKUP_GUIDE in combined
    assert "<spell>" in CARTESIA_MARKUP_GUIDE


def test_empty_tweak_is_no_op() -> None:
    assert build_instructions(PersonaTweak()) == build_instructions(None)
    assert resolve_greeting(PersonaTweak()) == GREETING


def test_extra_instructions_appended_before_cartesia_guide() -> None:
    out = build_instructions(PersonaTweak(extra_instructions="Confirm member ID twice."))
    assert out.startswith(BASE_PERSONA)
    assert "Confirm member ID twice." in out
    assert out.index("Confirm member ID twice.") < out.index(CARTESIA_MARKUP_GUIDE)


def test_greeting_override() -> None:
    assert resolve_greeting(PersonaTweak(greeting="Hello there.")) == "Hello there."


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
        ' "ivr_playbook": {"rep_keyword": "Advocate"}}'
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
