"""Leak canary — the backstop against the worst-case failure (PHI to the LLM untokenized).

Runs on the *tokenized* text, after substitution. If a PHI-shaped string survives,
something was missed. It scans for residual shapes independent of the detectors, so a
detector gap doesn't blind the canary. Findings are raised as an alert and (optionally)
fail the turn closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..tokens.token import TOKEN_RE

# Residual-PHI shapes. These run on text where legitimate values are already tokens,
# so any hit is suspicious by construction.
_CANARY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn_like", re.compile(r"\b\d{3}[- .]\d{2}[- .]\d{4}\b")),
    ("long_digit_run", re.compile(r"\b\d{9,}\b")),
    ("phone_like", re.compile(r"\b\d{3}[- .]\d{3}[- .]\d{4}\b")),
    ("email_like", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("alnum_id_like", re.compile(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,}\b")),
]


@dataclass(frozen=True)
class CanaryFinding:
    kind: str
    text: str
    start: int


@dataclass(frozen=True)
class CanaryResult:
    ok: bool
    findings: list[CanaryFinding]


def scan(tokenized_text: str) -> CanaryResult:
    """Scan tokenized text for residual PHI shapes, ignoring the tokens themselves."""
    # Blank out token spans so e.g. [[MEMBER_ID_12]] doesn't trip long_digit_run.
    masked = TOKEN_RE.sub(lambda m: " " * (m.end() - m.start()), tokenized_text)

    findings: list[CanaryFinding] = []
    for kind, pat in _CANARY_PATTERNS:
        for m in pat.finditer(masked):
            findings.append(CanaryFinding(kind=kind, text=m.group(0), start=m.start()))

    return CanaryResult(ok=not findings, findings=findings)
