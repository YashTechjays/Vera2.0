"""Password hashing for the local `password` provider.

bcrypt (ADR §3.5.3; `user_identity.hashed_password` is String(255)). Verification
is constant-time via bcrypt.checkpw. The hash is a credential, never PHI — but it
still must never be logged or returned. bcrypt caps the input at 72 bytes; the
login endpoint rejects longer passwords rather than silently truncating.
"""

import bcrypt

MAX_PASSWORD_BYTES = 72
_ROUNDS = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    """True iff `password` matches `hashed`. Never raises on malformed input —
    a bad stored hash or an over-length password is simply a non-match."""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


# A dummy hash generated once at import from the SAME _ROUNDS as real hashes, so a
# verify against it costs exactly what a real verify costs (the bcrypt cost factor
# drives timing). The password is a throwaway, not a secret.
_DUMMY_HASH = hash_password("vera-timing-equalizer")


def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    """Constant-work verify: when there is no stored hash (unknown email, or a user
    with no password identity), still run a full bcrypt comparison against a dummy
    hash and return False. Keeps the unknown-email path the same latency as the
    wrong-password path — closes the login user-enumeration timing side-channel."""
    return verify_password(password, hashed if hashed is not None else _DUMMY_HASH)
