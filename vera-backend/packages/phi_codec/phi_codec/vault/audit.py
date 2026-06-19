"""Append-only audit log — the debug/compliance surface.

Records one event per detected/tokenized entity: which type, which recognizer
fired, the confidence, the turn, and a timestamp. Raw values are stored ONLY as
ciphertext (for bounded-retention re-id debugging); they never appear in plaintext
logs. The prototype keeps events in memory; production swaps in a Postgres-backed
implementation behind the same ``record``/``events`` interface.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .crypto import Encryptor, FernetEncryptor


@dataclass(frozen=True)
class AuditEvent:
    ts: float
    session_id: str
    turn_id: str
    direction: str  # "tokenize" | "reidentify"
    token: str
    entity_type: str
    recognizer: str
    score: float
    raw_ciphertext: bytes | None  # encrypted; None for reidentify lookups

    def redacted(self) -> dict:
        """Serializable view with no plaintext PHI (drops the ciphertext bytes)."""
        d = asdict(self)
        d.pop("raw_ciphertext", None)
        return d


class AuditLog:
    def __init__(self, encryptor: Encryptor | None = None) -> None:
        self._enc = encryptor or FernetEncryptor()
        self._events: list[AuditEvent] = []

    def record(
        self,
        *,
        session_id: str,
        turn_id: str,
        direction: str,
        token: str,
        entity_type: str,
        recognizer: str,
        score: float,
        raw_value: str | None = None,
    ) -> None:
        self._events.append(
            AuditEvent(
                ts=time.time(),
                session_id=session_id,
                turn_id=turn_id,
                direction=direction,
                token=token,
                entity_type=entity_type,
                recognizer=recognizer,
                score=score,
                raw_ciphertext=self._enc.encrypt(raw_value) if raw_value is not None else None,
            )
        )

    def events(self, session_id: str | None = None) -> list[AuditEvent]:
        if session_id is None:
            return list(self._events)
        return [e for e in self._events if e.session_id == session_id]
