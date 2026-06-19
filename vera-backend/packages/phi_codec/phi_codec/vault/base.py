"""Vault interface + the per-session mapping it manages.

The vault is the bidirectional, deterministic, lossless mapping between raw PHI
values and ``[[TYPE_N]]`` tokens. Raw values are held encrypted at rest; the same
raw value within a session always yields the same token (dedup via the forward map).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class VaultEntry:
    """One raw<->token binding plus the audit metadata captured at detection time."""

    token: str  # "[[MEMBER_ID_1]]"
    raw_value: str  # exact canonical value (normalized), used for tool-call re-id + TTS input
    entity_type: str
    first_turn_id: str
    recognizer: str
    score: float


class PHIVault(abc.ABC):
    """Async, session-scoped token vault.

    Implementations MUST serialize access per session (the mapping is not
    thread-safe, and dedup requires read-modify-write atomicity).
    """

    @abc.abstractmethod
    async def open_session(self, session_id: str) -> None: ...

    @abc.abstractmethod
    async def close_session(self, session_id: str) -> None:
        """Destroy the hot mapping for a session (call end)."""

    @abc.abstractmethod
    async def get_or_create_token(
        self,
        session_id: str,
        entity_type: str,
        raw_value: str,
        *,
        turn_id: str,
        recognizer: str,
        score: float,
    ) -> str:
        """Return the stable token for ``raw_value``, creating it on first sight.

        Same (session, normalized raw_value) -> same token. The per-type counter
        only advances for genuinely new values.
        """

    @abc.abstractmethod
    async def resolve(self, session_id: str, token: str) -> VaultEntry | None:
        """Look up a token surface string back to its entry, or None if unknown."""

    @abc.abstractmethod
    async def dump(self, session_id: str) -> list[VaultEntry]:
        """All entries for a session (for the UI vault panel / debugging)."""

    @abc.abstractmethod
    async def touch(self, session_id: str) -> None:
        """Extend the session TTL on activity."""
