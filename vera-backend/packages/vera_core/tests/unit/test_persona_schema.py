import pytest
from pydantic import ValidationError

from vera_core.schemas import PersonaTweak


def test_empty_tweak_is_all_none() -> None:
    t = PersonaTweak()
    assert t.extra_instructions is None
    assert t.greeting is None


def test_round_trip_excludes_none() -> None:
    t = PersonaTweak(extra_instructions="Always confirm the member ID twice.")
    assert t.model_dump(exclude_none=True) == {
        "extra_instructions": "Always confirm the member ID twice."
    }


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        PersonaTweak.model_validate({"tone": "formal"})


def test_length_caps_enforced() -> None:
    with pytest.raises(ValidationError):
        PersonaTweak(extra_instructions="x" * 4001)
    with pytest.raises(ValidationError):
        PersonaTweak(greeting="y" * 501)
