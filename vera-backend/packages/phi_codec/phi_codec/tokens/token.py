"""PHI token surface syntax: ``[[TYPE_N]]``.

Double-square-bracketed, ALL-CAPS type, underscore, 1-based index. Chosen to
survive an LLM round-trip: rare in natural language, visually atomic, trivially
regex-recoverable, and easy to instruct the model never to alter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches a single token and captures (TYPE, index). TYPE allows A-Z and underscore
# so multi-word types like MEMBER_ID round-trip; index is the per-type counter.
TOKEN_RE = re.compile(r"\[\[([A-Z][A-Z_]*)_(\d+)\]\]")


@dataclass(frozen=True)
class PHIToken:
    entity_type: str  # e.g. "NAME", "MEMBER_ID"
    index: int  # 1-based, per type, per session

    @property
    def surface(self) -> str:
        return f"[[{self.entity_type}_{self.index}]]"

    def __str__(self) -> str:  # so f-strings / str() give the wire form
        return self.surface

    @classmethod
    def parse(cls, text: str) -> "PHIToken | None":
        """Parse a single token surface string, or None if it isn't one."""
        m = TOKEN_RE.fullmatch(text.strip())
        if not m:
            return None
        return cls(entity_type=m.group(1), index=int(m.group(2)))


def find_tokens(text: str) -> list[PHIToken]:
    """All tokens appearing in ``text``, in order of appearance (with repeats)."""
    return [PHIToken(m.group(1), int(m.group(2))) for m in TOKEN_RE.finditer(text)]


# Lenient matcher for repairing LLM-mangled tokens on the re-identify path: tolerates
# single brackets, internal spaces, lowercase, and space/dash separators —
# "[[ name 1 ]]", "[NAME-1]", "[[member id_2]]". Empirically Gemini doesn't mangle the
# [[TYPE_N]] form, but this is the defense-in-depth backstop for the rare tail.
LENIENT_TOKEN_RE = re.compile(r"\[\[?\s*([A-Za-z][A-Za-z _\-]*?)\s*[_\-\s](\d+)\s*\]\]?")


def canonical_token(type_part: str, index: str | int) -> str:
    """Normalize a mangled (type, index) to the canonical ``[[TYPE_N]]`` surface."""
    t = re.sub(r"[ \-]+", "_", type_part.strip()).upper()
    t = re.sub(r"_+", "_", t).strip("_")
    return f"[[{t}_{int(index)}]]"
