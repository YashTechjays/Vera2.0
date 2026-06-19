"""In-process vault — the default backend for the prototype and tests.

Mirrors the semantics the Redis backend will have (per-session isolation, dedup,
encrypted-at-rest raw values, per-session lock) without external infra. Raw values
are stored as ciphertext even in memory so the encrypt/decrypt path is real and the
UI can demonstrate masked vs. revealed values.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .base import PHIVault, VaultEntry
from .crypto import Encryptor, FernetEncryptor


@dataclass
class _StoredEntry:
    token: str
    ciphertext: bytes  # encrypted raw value
    entity_type: str
    first_turn_id: str
    recognizer: str
    score: float


@dataclass
class _Session:
    forward: dict[str, str] = field(default_factory=dict)  # norm raw value -> token
    reverse: dict[str, _StoredEntry] = field(default_factory=dict)  # token -> stored
    counters: dict[str, int] = field(default_factory=dict)  # entity_type -> last index
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InMemoryVault(PHIVault):
    def __init__(self, encryptor: Encryptor | None = None) -> None:
        self._enc = encryptor or FernetEncryptor()
        self._sessions: dict[str, _Session] = {}

    @property
    def encryptor(self) -> Encryptor:
        return self._enc

    def _session(self, session_id: str) -> _Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(f"session {session_id!r} is not open")
        return sess

    async def open_session(self, session_id: str) -> None:
        self._sessions.setdefault(session_id, _Session())

    async def close_session(self, session_id: str) -> None:
        # Drop all references; ciphertext + keys go with it.
        self._sessions.pop(session_id, None)

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
        sess = self._session(session_id)
        async with sess.lock:
            existing = sess.forward.get(raw_value)
            if existing is not None:
                return existing

            idx = sess.counters.get(entity_type, 0) + 1
            sess.counters[entity_type] = idx
            token = f"[[{entity_type}_{idx}]]"

            sess.forward[raw_value] = token
            sess.reverse[token] = _StoredEntry(
                token=token,
                ciphertext=self._enc.encrypt(raw_value),
                entity_type=entity_type,
                first_turn_id=turn_id,
                recognizer=recognizer,
                score=score,
            )
            return token

    async def resolve(self, session_id: str, token: str) -> VaultEntry | None:
        sess = self._session(session_id)
        stored = sess.reverse.get(token)
        if stored is None:
            return None
        return VaultEntry(
            token=stored.token,
            raw_value=self._enc.decrypt(stored.ciphertext),
            entity_type=stored.entity_type,
            first_turn_id=stored.first_turn_id,
            recognizer=stored.recognizer,
            score=stored.score,
        )

    async def dump(self, session_id: str) -> list[VaultEntry]:
        sess = self._session(session_id)
        return [
            VaultEntry(
                token=s.token,
                raw_value=self._enc.decrypt(s.ciphertext),
                entity_type=s.entity_type,
                first_turn_id=s.first_turn_id,
                recognizer=s.recognizer,
                score=s.score,
            )
            for s in sess.reverse.values()
        ]

    async def touch(self, session_id: str) -> None:
        # No TTL to extend in-process; method exists for interface parity with Redis.
        self._session(session_id)
