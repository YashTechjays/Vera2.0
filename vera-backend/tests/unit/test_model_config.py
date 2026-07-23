import pytest
from pydantic import ValidationError

from vera_core.services.model_config import (
    InvalidModelName,
    InvalidThinkingOverride,
    ThinkingOverride,
    is_gemini_3_model,
    normalize_model_name,
    validate_extra_config,
)


def test_trims_surrounding_whitespace() -> None:
    assert normalize_model_name("  gemini-3.5-flash  ") == "gemini-3.5-flash"


def test_rejects_empty() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("")


def test_rejects_whitespace_only() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("   ")


def test_rejects_too_long() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("g" * 201)


def test_accepts_max_length() -> None:
    name = "g" * 200
    assert normalize_model_name(name) == name


def test_rejects_disallowed_characters() -> None:
    with pytest.raises(InvalidModelName):
        normalize_model_name("gemini 3.5 flash")  # spaces not allowed


def test_accepts_dots_hyphens_underscores() -> None:
    assert normalize_model_name("gemini-3.1_flash-lite") == "gemini-3.1_flash-lite"


def test_is_gemini_3_model_matches_suggested_gemini_3_names() -> None:
    for model in ("gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "GEMINI-3-PRO"):
        assert is_gemini_3_model(model) is True


def test_is_gemini_3_model_false_for_pre_3() -> None:
    for model in ("gemini-2.5-flash", "gemini-1.5-pro"):
        assert is_gemini_3_model(model) is False


def test_thinking_override_rejects_both_fields_set() -> None:
    with pytest.raises(ValidationError):
        ThinkingOverride(thinking_budget=0, thinking_level="low")


def test_thinking_override_rejects_neither_field_set() -> None:
    with pytest.raises(ValidationError):
        ThinkingOverride()


def test_thinking_override_accepts_budget_only() -> None:
    assert ThinkingOverride(thinking_budget=500).thinking_budget == 500


def test_thinking_override_accepts_level_only() -> None:
    assert ThinkingOverride(thinking_level="high").thinking_level == "high"


def test_validate_extra_config_accepts_none() -> None:
    validate_extra_config("gemini-2.5-flash", None)  # no raise


def test_validate_extra_config_accepts_matching_pairs() -> None:
    validate_extra_config("gemini-2.5-flash", ThinkingOverride(thinking_budget=0))
    validate_extra_config("gemini-3.5-flash", ThinkingOverride(thinking_level="low"))


def test_validate_extra_config_rejects_level_on_pre_3_model() -> None:
    with pytest.raises(InvalidThinkingOverride):
        validate_extra_config("gemini-2.5-flash", ThinkingOverride(thinking_level="low"))


def test_validate_extra_config_rejects_budget_on_gemini_3_model() -> None:
    with pytest.raises(InvalidThinkingOverride):
        validate_extra_config("gemini-3.5-flash", ThinkingOverride(thinking_budget=0))
