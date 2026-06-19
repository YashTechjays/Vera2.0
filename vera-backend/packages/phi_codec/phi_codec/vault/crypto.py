"""Envelope-encryption helpers.

In production the data-encryption key (DEK) is wrapped by a US-region Cloud KMS
key (KEK) and raw PHI is encrypted at rest in Redis and the audit log. For the
prototype we stand in a local Fernet key so the encrypt/decrypt path is exercised
end-to-end (and demonstrable in the test UI) without external infra.

Swap ``FernetEncryptor`` for a ``KmsEnvelopeEncryptor`` later; the ``Encryptor``
Protocol is all the vault depends on.
"""

from __future__ import annotations

from typing import Protocol

from cryptography.fernet import Fernet


class Encryptor(Protocol):
    def encrypt(self, plaintext: str) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> str: ...

    @property
    def scheme(self) -> str: ...


class FernetEncryptor:
    """AES-128-CBC + HMAC (Fernet) — dev/prototype stand-in for KMS envelope encryption."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or Fernet.generate_key()
        self._f = Fernet(self._key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._f.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        return self._f.decrypt(ciphertext).decode("utf-8")

    @property
    def scheme(self) -> str:
        return "fernet-aes128-cbc-hmac (dev stand-in for KMS envelope)"


class NullEncryptor:
    """No-op, for tests that assert on raw values directly. Never use in prod."""

    def encrypt(self, plaintext: str) -> bytes:
        return plaintext.encode("utf-8")

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")

    @property
    def scheme(self) -> str:
        return "null (plaintext — test only)"
