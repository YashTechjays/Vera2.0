"""Pure helpers that turn detections into tokenized text.

Kept side-effect free (no vault, no I/O) so they're trivially testable. The codec
interleaves the async vault calls between ``resolve_overlaps`` and ``apply_replacements``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import EntityType
from ..detection.engine import Detection

# On a score tie, prefer the more specific/sensitive identifier. A 9-digit string
# tagged both US_SSN and DATE_TIME should resolve to SSN, not DATE. Higher = preferred.
_SPECIFICITY: dict[EntityType, int] = {
    EntityType.SSN: 100,
    EntityType.MBI: 95,
    EntityType.LICENSE: 88,
    EntityType.BENEFICIARY_ID: 85,
    EntityType.MRN: 80,
    EntityType.ACCOUNT: 75,
    EntityType.FAX: 72,  # beats PHONE on tie when "fax" cue present
    EntityType.PHONE: 70,
    EntityType.EMAIL: 66,
    EntityType.URL: 64,
    EntityType.IP_ADDRESS: 62,
    EntityType.DEVICE_SERIAL: 60,
    EntityType.VEHICLE: 58,
    EntityType.ZIP_CODE: 55,
    EntityType.AGE_OVER_89: 52,
    EntityType.STREET_ADDRESS: 50,
    EntityType.CITY: 45,
    EntityType.DATE: 40,
    EntityType.UNIQUE_CODE: 25,  # catch-all: specific types win on tie
    EntityType.NAME: 20,
}


@dataclass(frozen=True)
class Replacement:
    start: int
    end: int
    token: str


# Only these types are atomic wholes that should swallow anything inside them
# regardless of score (a URL/email must never be tokenized in fragments). Fuzzy NER
# spans (e.g. a broad GLiNER location) must NOT — otherwise a low-confidence wide span
# would override precise high-confidence regex hits it happens to contain.
_CONTAINER_TYPES = {EntityType.URL, EntityType.EMAIL}


def _strictly_contains(outer: Detection, inner: Detection) -> bool:
    same_span = outer.start == inner.start and outer.end == inner.end
    return outer.start <= inner.start and outer.end >= inner.end and not same_span


def resolve_overlaps(detections: list[Detection]) -> list[Detection]:
    """Drop overlapping spans, keeping the strongest whole identifier.

    Two phases:
    1. Containment — a span strictly inside an atomic container type (URL, EMAIL) is a
       fragment (e.g. the 99281 inside a URL). Keep the container regardless of score so
       we never tokenize only part of it and leak the rest.
    2. Score — among the remaining (equal-span or partially overlapping) spans, keep
       the higher score; ties break toward the more specific identifier, then the
       longer span, then the earlier start, for determinism.
    """
    # Phase 1: drop fragments strictly contained in an atomic container.
    survivors = [
        d for d in detections
        if not any(
            o.entity_type in _CONTAINER_TYPES and _strictly_contains(o, d)
            for o in detections if o is not d
        )
    ]

    # Phase 2: greedy by score/specificity over the survivors.
    ordered = sorted(
        survivors,
        key=lambda d: (
            -d.score,
            -_SPECIFICITY.get(d.entity_type, 0),
            -(d.end - d.start),
            d.start,
            d.entity_type.value,
        ),
    )
    kept: list[Detection] = []
    for d in ordered:
        if any(not (d.end <= k.start or d.start >= k.end) for k in kept):
            continue  # overlaps something already kept (which is >= this one)
        kept.append(d)
    return sorted(kept, key=lambda d: d.start)


def apply_replacements(text: str, replacements: list[Replacement]) -> str:
    """Substitute spans right-to-left so earlier offsets stay valid."""
    out = text
    for r in sorted(replacements, key=lambda r: r.start, reverse=True):
        out = out[: r.start] + r.token + out[r.end :]
    return out
