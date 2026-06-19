"""TTS readback formatting — IDs must be voiced char-by-char, phones grouped."""

import pytest

from phi_codec.config import EntityType
from phi_codec.formatting.tts import format_for_tts


@pytest.mark.parametrize(
    "etype, raw, expected",
    [
        (EntityType.BENEFICIARY_ID, "XYZ987654321", "X Y Z 9 8 7 6 5 4 3 2 1"),
        (EntityType.SSN, "521238765", "5 2 1 2 3 8 7 6 5"),
        (EntityType.PHONE, "2125551234", "212 555 1234"),
        (EntityType.PHONE, "12125551234", "1 212 555 1234"),
        (EntityType.MBI, "3XW2P99UX19", "3 X W 2 P 9 9 U X 1 9"),
    ],
)
def test_spelled_and_grouped(etype, raw, expected):
    assert format_for_tts(etype, raw) == expected


@pytest.mark.parametrize("etype", [EntityType.NAME, EntityType.DATE, EntityType.CITY])
def test_natural_language_types_pass_through(etype):
    assert format_for_tts(etype, "John Smith") == "John Smith"
