"""Unit tests for the API-key hashing/token helpers (no DB)."""

from uuid import UUID

from control_plane.auth import api_key

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
KEY = UUID("00000000-0000-0000-0000-0000000000bb")


def test_hash_verify_roundtrip() -> None:
    salt = api_key.new_salt()
    secret = api_key.new_secret()
    key_hash = api_key.hash_secret(salt, secret)
    assert api_key.verify_secret(salt, secret, key_hash) is True


def test_verify_rejects_wrong_secret() -> None:
    salt = api_key.new_salt()
    key_hash = api_key.hash_secret(salt, api_key.new_secret())
    assert api_key.verify_secret(salt, api_key.new_secret(), key_hash) is False


def test_hash_is_salt_dependent() -> None:
    secret = api_key.new_secret()
    assert api_key.hash_secret(api_key.new_salt(), secret) != api_key.hash_secret(
        api_key.new_salt(), secret
    )


def test_format_parse_roundtrip() -> None:
    secret = api_key.new_secret()
    token = api_key.format_token(TENANT, KEY, secret)
    parsed = api_key.parse_token(token)
    assert parsed is not None
    assert parsed.tenant_id == TENANT
    assert parsed.key_id == KEY
    assert parsed.secret == secret


def test_parse_rejects_malformed() -> None:
    assert api_key.parse_token("nope") is None  # missing prefix
    assert api_key.parse_token("vk_only.two") is None  # too few parts
    assert api_key.parse_token(f"vk_not-a-uuid.{KEY}.sek") is None  # bad tenant uuid
    assert api_key.parse_token(f"vk_{TENANT}.{KEY}.") is None  # empty secret


def test_tampered_token_does_not_verify() -> None:
    salt = api_key.new_salt()
    secret = api_key.new_secret()
    key_hash = api_key.hash_secret(salt, secret)
    token = api_key.format_token(TENANT, KEY, secret)
    parsed = api_key.parse_token(token + "x")  # tamper the secret tail
    assert parsed is not None
    assert api_key.verify_secret(salt, parsed.secret, key_hash) is False
