from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    GREETING,
    SYSTEM_PROMPT,
    build_instructions,
    parse_persona_tweak,
    resolve_greeting,
)
from vera_core.schemas import PersonaTweak


def test_prompt_is_chat_only_and_includes_cartesia_guide() -> None:
    assert "record_service_coverage" not in SYSTEM_PROMPT
    assert "end_call" in SYSTEM_PROMPT
    assert "infertility" in SYSTEM_PROMPT.lower()
    assert "diagnostic testing" in SYSTEM_PROMPT.lower()
    assert GREETING.startswith("Hi, I'm calling on behalf of a patient")
    combined = build_instructions()
    assert combined.startswith(SYSTEM_PROMPT)
    assert CARTESIA_MARKUP_GUIDE in combined
    assert "<spell>" in CARTESIA_MARKUP_GUIDE


def test_empty_tweak_is_no_op() -> None:
    assert build_instructions(PersonaTweak()) == build_instructions(None)
    assert resolve_greeting(PersonaTweak()) == GREETING


def test_extra_instructions_appended_before_cartesia_guide() -> None:
    out = build_instructions(PersonaTweak(extra_instructions="Confirm member ID twice."))
    assert out.startswith(SYSTEM_PROMPT)
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
    # no nested key (e.g. Voice Lab) → no-op tweak, not an error
    assert parse_persona_tweak('{"wait_for_speaker": true}') == PersonaTweak()
