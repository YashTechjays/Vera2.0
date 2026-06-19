"""Coverage for the Safe Harbor identifier types added in the taxonomy pass."""

import pytest

pytestmark = pytest.mark.asyncio


def _by_type(entities):
    out = {}
    for e in entities:
        out.setdefault(e.entity_type, []).append(e.raw_text)
    return out


async def test_full_geographic_split(codec):
    await codec.open_session("g")
    t = await codec.tokenize("g", "Patient resides at 123 Magnolia Lane, Orlando, FL 32801.", turn_id="t1")
    types = _by_type(t.entities)
    assert "123 Magnolia Lane" in types.get("STREET_ADDRESS", [])
    assert "Orlando" in types.get("CITY", [])
    assert "32801" in types.get("ZIP_CODE", [])
    # State abbreviation is retained per Safe Harbor.
    assert "FL" in t.text_tokenized
    assert t.leak_ok


async def test_url_tokenized_as_whole_not_inner_id(codec):
    await codec.open_session("u")
    t = await codec.tokenize("u", "Guidelines at https://payer.com/auth/id-99281 now.", turn_id="t1")
    # The whole URL is one token; the interior 99281 must NOT leak.
    assert "[[URL_1]]" in t.text_tokenized
    assert "99281" not in t.text_tokenized
    assert "payer.com" not in t.text_tokenized


async def test_ip_address(codec):
    await codec.open_session("i")
    t = await codec.tokenize("i", "system log routing 192.168.1.254", turn_id="t1")
    assert "192.168.1.254" in _by_type(t.entities).get("IP_ADDRESS", [])


async def test_fax_distinguished_from_phone_by_context(codec):
    await codec.open_session("f")
    t = await codec.tokenize("f", "Fax the clinical notes to 800-555-0122.", turn_id="t1")
    assert "[[FAX_1]]" in t.text_tokenized


async def test_license_npi_and_state(codec):
    await codec.open_session("l")
    t = await codec.tokenize("l", "physician NPI is 1245319599 and state license is MD-88391", turn_id="t1")
    licenses = _by_type(t.entities).get("LICENSE", [])
    assert "1245319599" in licenses
    assert "MD-88391" in licenses


async def test_device_serial_keeps_model(codec):
    await codec.open_session("d")
    t = await codec.tokenize("d", "insulin pump model MM-780G, Serial: 99823.", turn_id="t1")
    assert "[[DEVICE_SERIAL_1]]" in t.text_tokenized
    assert "MM-780G" in t.text_tokenized  # model retained for medical necessity


async def test_unique_code_catchall(codec):
    await codec.open_session("c")
    t = await codec.tokenize("c", "the prior authorization token is PA-44129-X", turn_id="t1")
    assert "PA-44129-X" in _by_type(t.entities).get("UNIQUE_CODE", [])


async def test_age_over_89_generalized_but_not_under(codec):
    await codec.open_session("a")
    over = await codec.tokenize("a", "the patient is 94 years old", turn_id="t1")
    assert "[[AGE_OVER_89_1]]" in over.text_tokenized
    # 88 is <= 89: not generalized as AGE_OVER_89.
    under = await codec.tokenize("a", "the patient is 88 years old", turn_id="t2")
    assert "AGE_OVER_89" not in under.text_tokenized
