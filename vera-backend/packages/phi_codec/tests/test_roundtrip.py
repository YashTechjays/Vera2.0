"""Lossless round-trip + the two re-identification paths."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_tokenize_then_reidentify_is_lossless(codec):
    await codec.open_session("s1")
    text = "calling for John Smith member X Y Z nine eight seven six five four three two one"
    tok = await codec.tokenize("s1", text, turn_id="t1")
    assert "[[BENEFICIARY_ID_1]]" in tok.text_tokenized
    assert "[[NAME_1]]" in tok.text_tokenized
    assert tok.leak_ok

    # Re-identify the exact token string the LLM would echo back.
    rid = await codec.reidentify("s1", tok.text_tokenized)
    assert rid.ok
    # The canonical (normalized) member id is read back, char-spaced for TTS.
    assert "X Y Z 9 8 7 6 5 4 3 2 1" in rid.text
    assert "John Smith" in rid.text


async def test_same_entity_same_token(codec):
    await codec.open_session("s2")
    t1 = await codec.tokenize("s2", "patient John Smith", turn_id="t1")
    t2 = await codec.tokenize("s2", "again John Smith called", turn_id="t2")
    name_token_1 = next(e.token for e in t1.entities if e.entity_type == "NAME")
    name_token_2 = next(e.token for e in t2.entities if e.entity_type == "NAME")
    assert name_token_1 == name_token_2 == "[[NAME_1]]"


async def test_tool_call_returns_exact_raw_not_tts_formatted(codec):
    await codec.open_session("s3")
    await codec.tokenize("s3", "member X Y Z nine eight seven six five four three two one", turn_id="t1")
    args = await codec.reidentify_args("s3", {"member_id": "[[BENEFICIARY_ID_1]]"})
    # Tool-call path must return the literal value the payer API expects (no spaces).
    assert args["member_id"] == "XYZ987654321"


async def test_reidentify_fails_closed_on_unknown_token(codec):
    await codec.open_session("s4")
    rid = await codec.reidentify("s4", "here is [[NAME_7]] which was never minted")
    assert not rid.ok
    assert "[[NAME_7]]" in rid.unresolved


async def test_nested_tool_args_are_resolved(codec):
    await codec.open_session("s5")
    await codec.tokenize("s5", "patient John Smith", turn_id="t1")
    args = await codec.reidentify_args("s5", {"patient": {"name": "[[NAME_1]]"}, "ids": ["[[NAME_1]]"]})
    assert args == {"patient": {"name": "John Smith"}, "ids": ["John Smith"]}
