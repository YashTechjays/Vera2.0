"""Spoken-form normalization — runs BEFORE detection.

STT output is messy: a member ID "XYZ987654321" arrives as "X Y Z nine eight
seven...", digits as words, letters spelled out or given in NATO phonetic
("B as in boy"). Regex/Presidio recognizers expect contiguous strings, so this
module collapses spoken identifier sequences back into canonical form.

The normalized text is what detection runs on and what (tokenized) reaches the
LLM. The original transcript is never needed downstream — re-identification
returns the canonical value, which is what we read back / send to the payer API.

This is intentionally conservative: it only collapses *runs* of spoken
letter/digit tokens (length >= 2), so ordinary prose ("a member", "I need") is
left untouched. Known residual risk: phonetic words that are also English words
(see MISNORMALIZATION_RISK below) — covered by tests and the leak canary.
"""

from __future__ import annotations

import re

# Spoken single digits. "oh"/"o" are 0 only inside a digit run (handled by run logic).
_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9",
}

# NATO/ICAO phonetic alphabet + common ad-hoc variants -> letter.
_PHONETIC: dict[str, str] = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I",
    "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L", "mike": "M",
    "november": "N", "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R",
    "sierra": "S", "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W",
    "xray": "X", "yankee": "Y", "zulu": "Z",
}

# Multipliers: "double seven" -> 77, "triple zero" -> 000.
_MULTIPLIERS: dict[str, int] = {"double": 2, "triple": 3}

# A token is a "spoken char" if it resolves to a single digit or letter.
_SINGLE_LETTER_RE = re.compile(r"^[a-z]$")


def _resolve_char(word: str) -> str | None:
    """Return the single canonical char a spoken token maps to, else None."""
    w = word.lower()
    if w in _DIGIT_WORDS:
        return _DIGIT_WORDS[w]
    if w in _PHONETIC:
        return _PHONETIC[w]
    if _SINGLE_LETTER_RE.match(w):
        return w.upper()
    return None


# "B as in boy" / "B like bravo" / "B for boy" -> "B". Applied before tokenizing so
# the spelled letter survives into the collapse pass.
_AS_IN_RE = re.compile(r"\b([A-Za-z])\s+(?:as in|like|for)\s+[A-Za-z]+\b", re.IGNORECASE)

# Strip filler that commonly punctuates spelled-out IDs without breaking the run.
_FILLER_WORDS = {"dash", "hyphen", "space"}


def normalize(text: str) -> str:
    """Collapse spoken identifier sequences in ``text`` into canonical form."""
    if not text:
        return text

    # 1) Resolve "<letter> as in <word>" phrases down to the bare letter.
    text = _AS_IN_RE.sub(lambda m: m.group(1).upper(), text)

    # 2) Tokenize on whitespace, keeping punctuation attached so prose is preserved.
    raw_tokens = text.split(" ")
    out: list[str] = []
    i = 0
    n = len(raw_tokens)

    while i < n:
        tok = raw_tokens[i]
        # Peel leading/trailing punctuation so "seven," still resolves; we re-attach.
        lead = ""
        core = tok
        m = re.match(r"^(\W*)(.*?)(\W*)$", tok, re.DOTALL)
        if m:
            lead, core = m.group(1), m.group(2)

        pending_mult = _MULTIPLIERS.get(core.lower())
        run_chars: list[str] = []
        run_trail = ""
        j = i

        # Greedily consume a run of spoken chars (with optional multipliers/fillers).
        while j < n:
            cj = raw_tokens[j]
            cm = re.match(r"^(\W*)(.*?)(\W*)$", cj, re.DOTALL)
            c_core, c_trail = (cm.group(2), cm.group(3)) if cm else (cj, "")
            low = c_core.lower()

            if low in _MULTIPLIERS:
                pending_mult = _MULTIPLIERS[low]
                j += 1
                continue
            if low in _FILLER_WORDS:
                j += 1
                continue

            ch = _resolve_char(c_core)
            if ch is None:
                break
            count = pending_mult or 1
            run_chars.extend([ch] * count)
            pending_mult = None
            run_trail = c_trail  # trailing punctuation of the last consumed token
            j += 1

        # A run is only collapsed if it has >= 2 resolved chars; otherwise the token
        # is ordinary prose ("a", "I", "oh") and is emitted verbatim.
        if len(run_chars) >= 2:
            out.append(lead + "".join(run_chars) + run_trail)
            i = j
        else:
            out.append(tok)
            i += 1

    return " ".join(out)


# Documentation of the known false-positive surface, surfaced in tests:
MISNORMALIZATION_RISK = (
    "Phonetic words that are also English words (e.g. 'mike', 'victor', 'golf', "
    "'india') can be collapsed to letters when they appear adjacent to other "
    "spoken chars. Mitigated by the >=2-char run requirement and downstream "
    "leak-canary checks; a production system would add a confidence/context model."
)
