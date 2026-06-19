"""Known-value seeding: match-known (tier-0) + detect-unknown (backstop)."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_seeded_value_matches_spoken_form_deterministically(codec):
    await codec.seed_session("seed1", {"BENEFICIARY_ID": "XYZ987654321", "NAME": "John Smith"})
    t = await codec.tokenize("seed1", "calling for John Smith member X Y Z nine eight seven six five four three two one", turn_id="t1")
    by_text = {e.raw_text: e for e in t.entities}
    assert by_text["XYZ987654321"].token == "[[BENEFICIARY_ID_1]]"
    assert by_text["XYZ987654321"].recognizer == "known"
    assert by_text["XYZ987654321"].score == 1.0
    assert by_text["John Smith"].recognizer == "known"


async def test_unexpected_phi_still_caught_by_backstop(codec):
    await codec.seed_session("seed2", {"NAME": "John Smith"})
    # The payer introduces an auth code + fax that were NOT seeded.
    t = await codec.tokenize("seed2", "John Smith, authorization PA-55021-Z fax 407-555-0122", turn_id="t1")
    types = {e.entity_type for e in t.entities}
    assert "UNIQUE_CODE" in types  # backstop caught the unseeded auth code
    assert "FAX" in types
    assert t.leak_ok


async def test_tool_call_reidentifies_to_exact_record_value(codec):
    await codec.seed_session("seed3", {"BENEFICIARY_ID": "XYZ987654321"})
    await codec.tokenize("seed3", "member X Y Z nine eight seven six five four three two one", turn_id="t1")
    args = await codec.reidentify_args("seed3", {"member_id": "[[BENEFICIARY_ID_1]]"})
    assert args["member_id"] == "XYZ987654321"  # record value, not STT transcription


async def test_seed_is_case_insensitive(codec):
    await codec.seed_session("seed4", {"NAME": "John Smith"})
    t = await codec.tokenize("seed4", "the caller is john smith today", turn_id="t1")
    assert "[[NAME_1]]" in t.text_tokenized


async def test_unknown_entity_type_raises(codec):
    with pytest.raises(ValueError):
        await codec.seed_session("seed5", {"NOT_A_TYPE": "x"})


async def test_phonetic_matches_stt_garbled_name_to_record_value(codec):
    await codec.seed_session("seed6", {"NAME": "Catherine Smith"})
    t = await codec.tokenize("seed6", "speaking with Kathryn Smyth about the claim", turn_id="t1")
    e = next(e for e in t.entities if e.entity_type == "NAME")
    assert e.recognizer == "known-phonetic"
    assert e.token == "[[NAME_1]]"
    # Re-identification returns the RECORD name, not the STT transcription.
    rid = await codec.reidentify("seed6", "[[NAME_1]]")
    assert rid.text == "Catherine Smith"


async def test_alias_and_suffix_keys_resolve_to_canonical(codec):
    # The user's own field names + JSON-friendly _N keys, all mapped by us.
    seeded = await codec.seed_session("seed8", {
        "BENEFICIARY_ID_1": "244523",
        "BENEFICIARY_ID_2": "AGXZ2434",   # JSON can't repeat a key → _N suffix
        "MEMBER_ID": "XYZ987654321",       # alias
        "GROUP_NUMBER": "GRP44821",        # alias → also BENEFICIARY_ID
        "PHONE_NUMBER": "4075550199",      # alias → PHONE
        "DOB": "04/05/1980",               # alias → DATE
    })
    types = [s["entity_type"] for s in seeded]
    assert types.count("BENEFICIARY_ID") == 4
    assert "PHONE" in types and "DATE" in types
    # distinct tokens for the four beneficiary IDs
    benef_tokens = {s["token"] for s in seeded if s["entity_type"] == "BENEFICIARY_ID"}
    assert len(benef_tokens) == 4


async def test_list_form_seeds_multiple_values(codec):
    seeded = await codec.seed_session("seed9", {"BENEFICIARY_ID": ["244523", "AGXZ2434", "XYZ987654321"]})
    assert len(seeded) == 3


async def test_phonetic_does_not_match_unrelated_words(codec):
    await codec.seed_session("seed7", {"NAME": "Catherine Smith"})
    t = await codec.tokenize("seed7", "the representative transferred the call", turn_id="t1")
    assert "NAME" not in {e.entity_type for e in t.entities}
