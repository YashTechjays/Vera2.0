"""Known-value matcher — tier-0 detection seeded from the patient record.

A provider initiates an eligibility call about a *specific, known* patient, so we
usually already hold that patient's PHI (name, DOB, member ID, SSN, address). Seeding
those values before the call turns de-identification of expected PHI from a detection
guess into a deterministic lookup: STT's "X Y Z nine eight seven…" normalizes to the
known member ID and matches exactly, yielding the pre-minted token with no reliance on
a recognizer firing.

Two passes per turn:
  1. Exact (case-insensitive) over the normalized transcript — handles IDs/SSN/dates,
     where spoken digit/letter forms already collapse to the canonical value.
  2. Phonetic (Metaphone + Jaro-Winkler) for free-text NAME/CITY only — catches STT
     garbles like "Kathryn" for a seeded "Catherine". Cheap and high-precision because
     it compares against THIS patient's handful of seeded values, not a global list.

This is a complement, never a replacement — PHI the payer introduces (names, auth
numbers, fax) isn't in the seed and must still be caught by the regex/GLiNER tiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import jellyfish

from ..config import EntityType
from .engine import Detection
from .normalizer import normalize

# Only free-text proper nouns get the phonetic pass; IDs/dates use exact-on-normalized.
_PHONETIC_TYPES = {EntityType.NAME, EntityType.CITY}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


@dataclass(frozen=True)
class KnownValue:
    entity_type: EntityType
    normalized: str  # canonical form matched against normalized transcript text
    token: str  # pre-minted token (so re-id returns the record value, not STT's guess)
    original: str  # the exact seeded value (what the payer API expects)


def _word_match(a: str, b: str, threshold: float) -> bool:
    """True if two words are equal, phonetically equal, or near-identical."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3 and jellyfish.metaphone(a) == jellyfish.metaphone(b):
        return True
    if min(len(a), len(b)) >= 4 and jellyfish.jaro_winkler_similarity(a, b) >= threshold:
        return True
    return False


class KnownValueIndex:
    """Per-session store of seeded patient PHI, with exact + phonetic matchers."""

    def __init__(self, *, phonetic: bool = True, phonetic_threshold: float = 0.9) -> None:
        self._sessions: dict[str, list[KnownValue]] = {}
        self._phonetic = phonetic
        self._threshold = phonetic_threshold

    def open(self, session_id: str) -> None:
        self._sessions.setdefault(session_id, [])

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def add(self, session_id: str, kv: KnownValue) -> None:
        self._sessions.setdefault(session_id, []).append(kv)

    def values(self, session_id: str) -> list[KnownValue]:
        return list(self._sessions.get(session_id, []))

    @staticmethod
    def canonicalize(value: str) -> str:
        return normalize(value)

    def match(self, session_id: str, normalized_text: str) -> list[Detection]:
        """Seeded values found in already-normalized text. Highest-trust detections."""
        known = sorted(self._sessions.get(session_id, []), key=lambda k: -len(k.normalized))
        taken: list[tuple[int, int]] = []
        detections: list[Detection] = []

        # Pass 1: exact (case-insensitive). Longest first so an ID isn't re-claimed.
        for kv in known:
            if not kv.normalized:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(kv.normalized)}(?!\w)", re.IGNORECASE)
            for m in pattern.finditer(normalized_text):
                if _overlaps(m.start(), m.end(), taken):
                    continue
                taken.append((m.start(), m.end()))
                detections.append(_known_det(kv, m.start(), m.end(), normalized_text, 1.0, "known"))

        # Pass 2: phonetic, for NAME/CITY only, over word windows not already claimed.
        if self._phonetic:
            words = [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(normalized_text)]
            for kv in known:
                if kv.entity_type not in _PHONETIC_TYPES:
                    continue
                target = _WORD_RE.findall(kv.normalized)
                if not target:
                    continue
                k = len(target)
                for i in range(len(words) - k + 1):
                    window = words[i : i + k]
                    if all(_word_match(target[j], window[j][0], self._threshold) for j in range(k)):
                        start, end = window[0][1], window[-1][2]
                        if _overlaps(start, end, taken):
                            continue
                        taken.append((start, end))
                        detections.append(
                            _known_det(kv, start, end, normalized_text, 0.9, "known-phonetic")
                        )
        return detections


def _overlaps(start: int, end: int, taken: list[tuple[int, int]]) -> bool:
    return any(not (end <= s or start >= e) for s, e in taken)


def _known_det(kv: KnownValue, start: int, end: int, text: str, score: float, rec: str) -> Detection:
    return Detection(
        entity_type=kv.entity_type,
        start=start,
        end=end,
        score=score,
        recognizer=rec,
        text=text[start:end],
        token=kv.token,
    )
