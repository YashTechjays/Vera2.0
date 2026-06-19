"""Unit tests for bcrypt password hashing."""

from control_plane.auth.password import MAX_PASSWORD_BYTES, hash_password, verify_password


def test_hash_then_verify() -> None:
    hashed = hash_password("hunter2")
    assert verify_password("hunter2", hashed)


def test_wrong_password_does_not_verify() -> None:
    assert not verify_password("nope", hash_password("hunter2"))


def test_each_hash_uses_a_fresh_salt() -> None:
    assert hash_password("same") != hash_password("same")


def test_malformed_stored_hash_is_a_non_match_not_an_error() -> None:
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_max_password_bytes_is_bcrypt_limit() -> None:
    assert MAX_PASSWORD_BYTES == 72
