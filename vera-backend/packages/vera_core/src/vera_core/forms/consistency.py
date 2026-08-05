"""Money-triplet numeric consistency: the pure checks behind NumericConsistency rules.

Backend half of a parity pair with the review UI's pass in
vera-frontend/src/lib/ibv/validation.ts — keep operand-skipping, tolerance and
message semantics in sync (same spirit as conditions.py ⇄ conditions.ts).
"""

import math
import re
from collections.abc import Mapping
from typing import Any

TRIPLET_KEYS: tuple[str, str, str] = ("total", "met_amount", "remaining")

_STRIP_RE = re.compile(r"[$,%\s]")


def parse_currency(value: str) -> float | None:
    """Parse a transcribed money string; None for specials, prose, or blanks."""
    cleaned = _STRIP_RE.sub("", value)
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def triplet_paths(base: str) -> tuple[str, str, str]:
    """The three leaf paths of the money triplet rooted at *base*."""
    return f"{base}.{TRIPLET_KEYS[0]}", f"{base}.{TRIPLET_KEYS[1]}", f"{base}.{TRIPLET_KEYS[2]}"


def _amount(answers: Mapping[str, Any], path: str) -> float | None:
    value = answers.get(path)
    return None if value is None else parse_currency(str(value))


def check_triplet(base: str, answers: Mapping[str, Any]) -> str | None:
    """Reason text when the triplet's recorded amounts are impossible, else None.

    Each check runs only when all of its operands parse as numbers; the sum check
    (±$0.01, compared in whole cents to dodge float noise) is skipped when an
    exceed check already fired — one clear clause beats a redundant second one.
    """
    total_path, met_path, remaining_path = triplet_paths(base)
    total = _amount(answers, total_path)
    met = _amount(answers, met_path)
    remaining = _amount(answers, remaining_path)

    clauses: list[str] = []
    if total is not None and met is not None and met > total:
        clauses.append(f"the met amount (${met:,.2f}) exceeds the total (${total:,.2f})")
    if total is not None and remaining is not None and remaining > total:
        clauses.append(
            f"the remaining amount (${remaining:,.2f}) exceeds the total (${total:,.2f})"
        )
    if (
        not clauses
        and total is not None
        and met is not None
        and remaining is not None
        and abs(round((met + remaining - total) * 100)) > 1
    ):
        clauses.append(
            f"the met amount (${met:,.2f}) plus the remaining amount (${remaining:,.2f}) "
            f"does not match the total (${total:,.2f})"
        )
    if not clauses:
        return None
    return "The recorded amounts are inconsistent: " + " and ".join(clauses) + "."
