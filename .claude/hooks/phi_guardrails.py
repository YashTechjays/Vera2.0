#!/usr/bin/env python3
"""
Vera PHI/HIPAA PreToolUse guardrail — hard-blocks the catastrophic PHI bright lines
on Write / Edit / MultiEdit / NotebookEdit *before* the edit is applied.

It inspects ONLY the text being ADDED (Write.content / Edit.new_string /
MultiEdit.edits[].new_string / NotebookEdit.new_source) and BLOCKS (exit code 2) when it
detects, by conservative heuristic:

  [phi-logging]   plaintext PHI passed to a logger / print / console / trace / span
  [phi-url]       PHI placed into a URL / route template / query string

These are HEURISTICS, deliberately conservative — a backstop for the rules in CLAUDE.md,
not a substitute. A false block is annoying; a missed PHI leak is not.

────────────────────────────────────────────────────────────────────────────────────────
OPT-OUT / OVERRIDE  (for a reviewed, legitimate case):
  • Disable everything:     export VERA_PHI_GUARD=off
  • Skip specific checks:   export VERA_PHI_GUARD_SKIP=phi-logging,phi-url
  • Exempt a single line:   end the line with   # phi-guard: allow
  • Remove entirely:        delete the PreToolUse entry in .claude/settings.json

Never scanned: comments, Markdown / CLAUDE.md, tests, and the vendored packages/phi_codec
tree. The hook fails OPEN on its own internal error (a guardrail bug must not brick editing).
────────────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import re
import sys

CHECKS = ("phi-logging", "phi-url")

# Raw-PHI-ish identifiers. In THIS domain, member/beneficiary names, SSN, DOB, MRN,
# diagnosis, addresses, etc. are PHI. Tokens ([[TYPE_N]]) and opaque *_id / *_token are not.
PHI_TERMS = re.compile(
    r"(?i)\b("
    r"ssn|social_security\w*|date_of_birth|dob|mrn|medical_record\w*|"
    r"patient_name|member_name|beneficiary_name|subscriber_name|dependent_name|"
    r"first_name|last_name|full_name|home_address|street_address|mailing_address|"
    r"phone_number|raw_value|raw_phi|plaintext\w*|phi_plain|diagnosis|icd\w*"
    r")\b"
)

# A token literal ([[...]]) is never raw PHI — exempt the line.
TOKENISH = re.compile(r"\[\[")

# Sinks that emit/observe: logging, print, console.*, OTel spans, Langfuse trace API.
SINK = re.compile(
    r"(?i)(logger|logging|log)\.\w+\(|(?<![\w.])print\(|console\.(log|info|warn|error|debug)\(|"
    r"\.set_attributes?\(|\.add_event\(|\.set_tag\(|"
    r"\.(span|trace|generation|event|update_current_span)\("
)

# URL / request construction context.
URL_CTX = re.compile(
    r"""(?i)https?://|["']/\w|urlencode|urljoin|params\s*=|query\s*=|"""
    r"""requests\.\w+\(|httpx\.\w+\(|fetch\(|\?\w+="""
)
ROUTE_DEC = re.compile(r"(?i)@\w+\.(get|post|put|patch|delete|websocket|route)\(")

COMMENT_PREFIX = ("#", "//", "*", "/*", "<!--")

_STRING_SPAN = re.compile(r"""(['"]).*?\1""")
_BRACE = re.compile(r"\{([^{}]*)\}")


class Violation:
    __slots__ = ("check", "lineno", "line", "message")

    def __init__(self, check: str, lineno: int, line: str, message: str) -> None:
        self.check = check
        self.lineno = lineno
        self.line = line.strip()
        self.message = message


def extract_added(data: dict) -> tuple[str, str]:
    """Return (file_path, added_text) from a PreToolUse payload — added text only."""
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path") or ""
    parts: list[str] = []
    for key in ("content", "new_string", "new_source"):
        if ti.get(key):
            parts.append(ti[key])
    for edit in ti.get("edits") or []:
        if (edit or {}).get("new_string"):
            parts.append(edit["new_string"])
    return fp, "\n".join(parts)


def should_skip_file(fp: str) -> bool:
    p = fp.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if "/.claude/" in p or p.startswith(".claude/"):
        return True
    if base == "CLAUDE.md" or p.endswith(".md"):
        return True
    if "packages/phi_codec/" in p:  # vendored; legitimately handles raw values
        return True
    if "/tests/" in p or base.startswith("test_") or base.endswith("_test.py"):
        return True
    if ".test." in base or ".spec." in base:
        return True
    return False


def _is_comment(line: str) -> bool:
    s = line.lstrip()
    return any(s.startswith(c) for c in COMMENT_PREFIX)


def _value_payload(line: str) -> str:
    """The parts of a line that carry a *value*: code outside string literals, plus the
    contents of any {…} interpolation fields. A PHI word that appears only inside a plain
    string literal (a label, not a value) is intentionally excluded."""
    braces = " ".join(_BRACE.findall(line))
    bare = _STRING_SPAN.sub(" ", line)
    return f"{bare} || {braces}"


def scan(fp: str, text: str, skip: "frozenset[str] | set[str]" = frozenset()) -> list[Violation]:
    out: list[Violation] = []
    for i, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or _is_comment(raw) or "phi-guard: allow" in raw:
            continue
        if TOKENISH.search(raw):
            continue

        phi_value = bool(PHI_TERMS.search(_value_payload(raw)))

        if "phi-logging" not in skip and phi_value and SINK.search(raw):
            out.append(Violation(
                "phi-logging", i, raw,
                "plaintext PHI in a log / print / console / trace / span. Scrub before emit; "
                "trace token/reference IDs, counts, and shapes — never raw values (tokenize "
                "via vera_core.phi.redact before any trace)."))
            continue

        if "phi-url" not in skip and phi_value:
            route_phi = ROUTE_DEC.search(raw) and _BRACE.search(raw)
            if route_phi or URL_CTX.search(raw):
                out.append(Violation(
                    "phi-url", i, raw,
                    "PHI in a URL / path / route / query string. URLs leak into browser "
                    "history, Referer headers, and access logs — use opaque UUIDs in paths, "
                    "never raw identifiers."))
                continue
    return out


def main() -> int:
    if os.environ.get("VERA_PHI_GUARD", "").strip().lower() in (
        "0", "off", "false", "no", "disable", "disabled"
    ):
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # not a parseable hook payload — do not interfere
    try:
        fp, text = extract_added(data)
        if not fp or not text or should_skip_file(fp):
            return 0
        skip = {s.strip() for s in os.environ.get("VERA_PHI_GUARD_SKIP", "").split(",") if s.strip()}
        violations = scan(fp, text, skip)
        if not violations:
            return 0

        lines = [
            "",
            "⛔ Vera PHI guardrail BLOCKED this edit — a PHI bright line was tripped.",
            f"   file: {fp}",
            "",
        ]
        for v in violations:
            lines.append(f"  [{v.check}] line {v.lineno}: {v.line}")
            lines.append(f"     → {v.message}")
            lines.append("")
        lines += [
            "If this is a reviewed, legitimate case, override one of these ways:",
            "  • end the offending line with   # phi-guard: allow",
            f"  • skip this check:   export VERA_PHI_GUARD_SKIP={violations[0].check}",
            "  • disable all:       export VERA_PHI_GUARD=off",
            "See CLAUDE.md for the rule. When unsure whether something is PHI, treat it as PHI.",
            "",
        ]
        sys.stderr.write("\n".join(lines))
        return 2
    except Exception as e:  # never brick editing on a guardrail bug — rules still live in CLAUDE.md
        sys.stderr.write(f"[phi-guardrails] internal error, allowing edit: {e}\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
