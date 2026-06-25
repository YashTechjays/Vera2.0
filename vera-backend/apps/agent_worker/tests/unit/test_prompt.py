from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    GREETING,
    IVR_NAVIGATOR_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_instructions,
    build_ivr_instructions,
    parse_persona_tweak,
    resolve_greeting,
)
from vera_core.schemas import PersonaTweak


def test_prompt_is_chat_only_and_includes_cartesia_guide() -> None:
    assert "record_service_coverage" not in SYSTEM_PROMPT
    assert "end_call" not in SYSTEM_PROMPT
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
    assert parse_persona_tweak('{"tone": "formal"}') == PersonaTweak()  # unknown key
    assert parse_persona_tweak('{"greeting": "Hi"}') == PersonaTweak(greeting="Hi")


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
    # STT-only this iteration: no DTMF / keypad instructions leaked in
    assert "dtmf" not in lower
    # assembly appends the Cartesia guide, like the chat persona
    combined = build_ivr_instructions()
    assert combined.startswith(IVR_NAVIGATOR_SYSTEM_PROMPT)
    assert CARTESIA_MARKUP_GUIDE in combined
