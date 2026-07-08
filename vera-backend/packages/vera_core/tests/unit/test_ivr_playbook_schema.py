import pytest
from pydantic import ValidationError

from vera_core.schemas import IvrPlaybookConfig


def test_strict_validation_on_write() -> None:
    # The write path stays strict: unknown keys and bad values are rejected outright.
    with pytest.raises(ValidationError):
        IvrPlaybookConfig.model_validate({"tone": "formal"})
    with pytest.raises(ValidationError):
        IvrPlaybookConfig.model_validate({"rep_keyword": "x" * 101})


def test_from_stored_is_lenient_and_never_raises() -> None:
    # A clean row round-trips unchanged.
    assert IvrPlaybookConfig.from_stored({"rep_keyword": "Advocate"}) == IvrPlaybookConfig(
        rep_keyword="Advocate"
    )
    # An unknown key is dropped, the rest survives.
    assert IvrPlaybookConfig.from_stored(
        {"legacy_key": "x", "rep_keyword": "Advocate"}
    ) == IvrPlaybookConfig(rep_keyword="Advocate")
    # A bad-value field (wrong type / over-length) is dropped, siblings survive.
    assert IvrPlaybookConfig.from_stored(
        {"rep_keyword": 123, "survey_answer": "Yes"}
    ) == IvrPlaybookConfig(survey_answer="Yes")
    assert IvrPlaybookConfig.from_stored(
        {"rep_keyword": "x" * 101, "survey_answer": "Yes"}
    ) == IvrPlaybookConfig(survey_answer="Yes")
    # Nothing salvageable → the empty no-op overlay (generic navigator), not an exception.
    assert IvrPlaybookConfig.from_stored({"rep_keyword": 123}) == IvrPlaybookConfig()
    assert IvrPlaybookConfig.from_stored({}) == IvrPlaybookConfig()
