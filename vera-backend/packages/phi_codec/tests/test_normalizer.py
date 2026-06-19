"""Tests for spoken-form normalization — the highest-risk module for false negatives."""

import pytest

from phi_codec.detection.normalizer import normalize


@pytest.mark.parametrize(
    "spoken, expected",
    [
        # digit-word runs collapse
        ("nine eight seven", "987"),
        ("my number is nine eight seven six", "my number is 9876"),
        # "oh" / "o" as zero inside a run
        ("five oh oh one", "5001"),
        # spelled letters collapse
        ("X Y Z", "XYZ"),
        # mixed letters + digits (classic member ID)
        ("X Y Z nine eight seven six five four three two one", "XYZ987654321"),
        # multipliers
        ("double seven three", "773"),
        ("triple zero one", "0001"),
        # NATO phonetic
        ("alpha bravo charlie", "ABC"),
        # "as in" spelling convention
        ("B as in boy R as in robert C as in cat", "BRC"),
        # fillers between spelled chars
        ("A dash one two three", "A123"),
    ],
)
def test_collapses_spoken_identifiers(spoken, expected):
    assert normalize(spoken) == expected


@pytest.mark.parametrize(
    "prose",
    [
        "I need to verify eligibility for the patient",
        "a member called about prior authorization",
        "can you confirm the coverage",
    ],
)
def test_leaves_ordinary_prose_untouched(prose):
    # Single stray letters/words ("a", "I", "to") must not be collapsed.
    assert normalize(prose) == prose


def test_empty_input():
    assert normalize("") == ""


def test_run_embedded_in_sentence():
    spoken = "the member id is X Y Z nine eight seven and the dob follows"
    assert normalize(spoken) == "the member id is XYZ987 and the dob follows"
