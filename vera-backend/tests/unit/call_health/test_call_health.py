"""Analyzer contract: JSON parsing/coercion, unassessable no-op, and the
prefix-stable chunked re-anchoring transcript window (prompt-cache rules)."""

from vera_core.call_health import (
    HEALTH_SYSTEM_PROMPT,
    HEALTH_USER_SUFFIX,
    HealthTranscript,
    parse_assessment,
)


def test_health_system_prompt_exists() -> None:
    assert isinstance(HEALTH_SYSTEM_PROMPT, str)
    assert len(HEALTH_SYSTEM_PROMPT) > 0


def test_parse_assessable_result() -> None:
    result = parse_assessment(
        '{"assessable": true, "call_health_score": 78, '
        '"intervention_flag": "none", "reason": "going fine"}'
    )
    assert result is not None
    assert (result.score, result.flag, result.reason) == (78, "none", "going fine")


def test_parse_unassessable_is_none() -> None:
    assert parse_assessment('{"assessable": false}') is None


def test_parse_strips_markdown_fences() -> None:
    result = parse_assessment(
        '```json\n{"assessable": true, "call_health_score": 40, '
        '"intervention_flag": "conversation_loop", "reason": "loop"}\n```'
    )
    assert result is not None
    assert result.flag == "conversation_loop"


def test_parse_clamps_score_and_coerces_unknown_flag() -> None:
    result = parse_assessment(
        '{"assessable": true, "call_health_score": 250, '
        '"intervention_flag": "stuck_in_loop", "reason": "?"}'
    )
    assert result is not None
    assert result.score == 100
    assert result.flag == "other"  # unknown vocabulary coerces, never propagates


def test_parse_missing_flag_reads_as_none() -> None:
    result = parse_assessment('{"assessable": true, "call_health_score": 90}')
    assert result is not None
    assert result.flag == "none"


def test_parse_assessable_without_score_is_none() -> None:
    assert parse_assessment('{"assessable": true, "intervention_flag": "none"}') is None


def test_parse_garbage_is_none() -> None:
    assert parse_assessment("I think the call is fine!") is None


def test_render_is_prefix_stable_while_under_the_cap() -> None:
    a, b = HealthTranscript(max_turns=60), HealthTranscript(max_turns=60)
    for i in range(10):
        a.add("agent", "bot", f"question {i}")
        a.add("user", "rep", f"answer {i}")
        b.add("agent", "bot", f"question {i}")
        b.add("user", "rep", f"answer {i}")
    shorter = a.render_user_message().removesuffix(HEALTH_USER_SUFFIX)
    b.add("user", "rep", "one more")
    longer = b.render_user_message().removesuffix(HEALTH_USER_SUFFIX)
    assert longer.startswith(shorter)  # cacheable prefix grows append-only


def test_window_reanchors_in_chunks_not_per_turn() -> None:
    t = HealthTranscript(max_turns=60)
    for i in range(61):  # one past the cap -> single truncation to newest 40
        t.add("user", "rep", f"turn {i}")
    assert t.turn_count == 40
    t.add("user", "rep", "turn 61")
    assert t.turn_count == 41  # grows again; no per-request sliding


def test_dtmf_turn_labelled_as_keypad() -> None:
    t = HealthTranscript(max_turns=60)
    t.add("dtmf", "bot", "3")
    assert "Vera (agent) [keypad]: 3" in t.render_user_message()


def test_health_settings_defaults() -> None:
    from vera_core.config.settings import Settings

    s = Settings(_env_file=None)
    assert s.health_primary_model == "google:gemini-3.1-flash-lite"
    assert s.health_fallback_models == ["openai:gpt-5.4-mini"]
    assert s.health_min_interval_seconds == 15.0
    assert s.health_min_user_turns == 2
    assert s.health_max_turns == 60
    assert s.health_attempt_timeout_seconds == 8.0
