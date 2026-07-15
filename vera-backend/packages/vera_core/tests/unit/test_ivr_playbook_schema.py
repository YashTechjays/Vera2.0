import pytest
from pydantic import ValidationError

from vera_core.schemas import IvrPlaybookConfig


def test_strict_validation_on_write() -> None:
    # The write path stays strict: unknown keys and bad values are rejected outright.
    with pytest.raises(ValidationError):
        IvrPlaybookConfig.model_validate({"tone": "formal"})
    with pytest.raises(ValidationError):
        IvrPlaybookConfig.model_validate({"provider_subflows": "x" * 1001})
    # A removed structured field is now an unknown key → rejected on the strict write path.
    with pytest.raises(ValidationError):
        IvrPlaybookConfig.model_validate({"rep_keyword": "Advocate"})


def test_from_stored_is_lenient_and_never_raises() -> None:
    # A clean row round-trips unchanged.
    assert IvrPlaybookConfig.from_stored({"extra_rules": "Say Advocate"}) == IvrPlaybookConfig(
        extra_rules="Say Advocate"
    )
    # An unknown key — including a removed structured field — is dropped, the rest survives.
    assert IvrPlaybookConfig.from_stored(
        {"rep_keyword": "Advocate", "extra_rules": "Say Advocate"}
    ) == IvrPlaybookConfig(extra_rules="Say Advocate")
    # A bad-value field (wrong type / over-length) is dropped, siblings survive.
    assert IvrPlaybookConfig.from_stored(
        {"provider_subflows": 123, "extra_rules": "Say Advocate"}
    ) == IvrPlaybookConfig(extra_rules="Say Advocate")
    assert IvrPlaybookConfig.from_stored(
        {"provider_subflows": "x" * 1001, "extra_rules": "Say Advocate"}
    ) == IvrPlaybookConfig(extra_rules="Say Advocate")
    # Nothing salvageable → the empty no-op overlay (generic navigator), not an exception.
    assert IvrPlaybookConfig.from_stored({"provider_subflows": 123}) == IvrPlaybookConfig()
    assert IvrPlaybookConfig.from_stored({}) == IvrPlaybookConfig()
