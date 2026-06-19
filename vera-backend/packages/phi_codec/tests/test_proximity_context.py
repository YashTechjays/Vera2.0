"""Proximity-weighted context: the nearest cue wins, far cues don't bleed across."""

import pytest

pytestmark = pytest.mark.asyncio


def _types(entities):
    return {e.raw_text: e.entity_type for e in entities}


async def test_contact_number_types_as_phone_despite_far_member_context(codec):
    await codec.open_session("p1")
    txt = "The member id is 244523 and group number is AGXZ2434 and you can reach the patient at 919912345."
    t = await codec.tokenize("p1", txt, turn_id="t1")
    types = _types(t.entities)
    # The local "reach ... at" cue must beat the far-away "member/group" cue.
    assert types.get("919912345") == "PHONE"
    # The genuinely member-anchored numbers stay member IDs.
    assert types.get("244523") == "BENEFICIARY_ID"
    assert types.get("AGXZ2434") == "BENEFICIARY_ID"
    assert t.leak_ok


async def test_adjacent_member_context_still_types_member(codec):
    await codec.open_session("p2")
    t = await codec.tokenize("p2", "member id 987654321 please", turn_id="t1")
    assert _types(t.entities).get("987654321") == "BENEFICIARY_ID"


async def test_adjacent_phone_context_still_types_phone(codec):
    await codec.open_session("p3")
    t = await codec.tokenize("p3", "the best callback number is 9199123456", turn_id="t1")
    assert _types(t.entities).get("9199123456") == "PHONE"
