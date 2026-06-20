from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    GREETING,
    SYSTEM_PROMPT,
    build_instructions,
)


def test_prompt_is_chat_only_and_includes_cartesia_guide() -> None:
    # chat-only: no tool machinery leaked into the prompt
    assert "record_service_coverage" not in SYSTEM_PROMPT
    assert "end_call" not in SYSTEM_PROMPT
    # persona + objective retained
    assert "infertility" in SYSTEM_PROMPT.lower()
    assert "diagnostic testing" in SYSTEM_PROMPT.lower()
    # greeting is the outbound opener
    assert GREETING.startswith("Hi, I'm calling on behalf of a patient")
    # assembly appends the Cartesia guide
    combined = build_instructions()
    assert combined.startswith(SYSTEM_PROMPT)
    assert CARTESIA_MARKUP_GUIDE in combined
    assert "<spell>" in CARTESIA_MARKUP_GUIDE
