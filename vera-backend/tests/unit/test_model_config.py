import pytest

from vera_core.services.model_config import InvalidModelName, normalize_model_name


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
