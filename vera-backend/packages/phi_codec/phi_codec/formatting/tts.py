"""Spoken-readback formatting for the re-identify -> TTS path.

When the agent reads an identifier back to the payer, Cartesia must speak it
*character by character* (or in the right digit groups), not as a cardinal number
("nine hundred eighty-seven million..."). This formatter is applied ONLY on the TTS
path; the tool-call path returns the exact raw value untouched (the payer API needs
the literal string).
"""

from __future__ import annotations

from ..config import EntityType

# Types that must be spelled/grouped for correct speech.
_SPELL_OUT = {
    EntityType.SSN,
    EntityType.BENEFICIARY_ID,
    EntityType.MRN,
    EntityType.LICENSE,
    EntityType.MBI,
    EntityType.ACCOUNT,
    EntityType.ZIP_CODE,
    EntityType.DEVICE_SERIAL,
    EntityType.VEHICLE,
    EntityType.UNIQUE_CODE,
}


def _spell_chars(value: str) -> str:
    """Space out each alphanumeric so it's voiced individually; drop separators."""
    return " ".join(c for c in value if c.isalnum())


def _group_phone(value: str) -> str:
    digits = [c for c in value if c.isdigit()]
    if len(digits) == 10:
        return f"{''.join(digits[:3])} {''.join(digits[3:6])} {''.join(digits[6:])}"
    if len(digits) == 11:
        return f"{digits[0]} {''.join(digits[1:4])} {''.join(digits[4:7])} {''.join(digits[7:])}"
    return _spell_chars(value)


def format_for_tts(entity_type: EntityType, raw_value: str) -> str:
    """Return a string TTS will pronounce correctly for this entity type."""
    if entity_type in (EntityType.PHONE, EntityType.FAX):
        return _group_phone(raw_value)
    if entity_type in _SPELL_OUT:
        return _spell_chars(raw_value)
    # NAME / DATE / ADDRESS / EMAIL read fine as natural language.
    return raw_value
