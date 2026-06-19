"""Secret resolution boundary.

Application code asks a SecretProvider for a named secret and never reads
credential env vars directly. Local dev uses EnvSecretProvider; production uses
Google Secret Manager (CMEK-encrypted) behind the same interface.
"""

import os
from typing import Protocol, runtime_checkable


class SecretNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret not found: {name}")
        self.name = name


@runtime_checkable
class SecretProvider(Protocol):
    def get(self, name: str) -> str:
        """Return the secret value for `name`; raise SecretNotFoundError if absent."""
        ...


class EnvSecretProvider:
    """Local dev: secrets come from environment variables (optionally prefixed)."""

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix

    def get(self, name: str) -> str:
        key = f"{self._prefix}{name}".upper().replace("-", "_")
        value = os.environ.get(key)
        if value is None:
            raise SecretNotFoundError(name)
        return value
