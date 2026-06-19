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
