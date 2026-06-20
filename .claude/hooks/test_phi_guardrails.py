#!/usr/bin/env python3
"""Sample-input tests for phi_guardrails — proves BLOCK + PASS for each check.

Run:  python3 .claude/hooks/test_phi_guardrails.py
Exit 0 = all passed. No pytest required.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import phi_guardrails as g  # type: ignore[import-not-found]  # noqa: E402

HOOK = os.path.join(HERE, "phi_guardrails.py")


def detected(fp: str, text: str, skip: "frozenset[str] | set[str]" = frozenset()) -> set[str]:
    """Mirror the hook's gating: skipped files yield no findings."""
    if g.should_skip_file(fp):
        return set()
    return {v.check for v in g.scan(fp, text, skip)}


# (name, file_path, added_text, expected_check or None)
CASES = [
    # ---- phi-logging ------------------------------------------------------------------
    ("phi-logging BLOCK: f-string into logger", "apps/control_plane/x.py",
     'logger.info(f"patient {patient_name} verified")', "phi-logging"),
    ("phi-logging BLOCK: PHI value into OTel span", "packages/vera_core/src/vera_core/x.py",
     'span.set_attribute("ssn", ssn)', "phi-logging"),
    ("phi-logging BLOCK: console.log template literal", "vera-frontend/app.tsx",
     "console.log(`patient ${patient_name}`)", "phi-logging"),
    ("phi-logging PASS: shape/counts only", "packages/vera_core/src/vera_core/x.py",
     'logger.info("redacted %d entities for session %s", n, session_id)', None),
    ("phi-logging PASS: PHI word is a static label, not a value", "x.py",
     'logger.warning("missing ssn for record")', None),
    ("phi-logging PASS: audit records field NAMES not values", "x.py",
     'audit.emit(detail={"fields": ["ssn", "dob"]})', None),

    # ---- phi-url ----------------------------------------------------------------------
    ("phi-url BLOCK: raw SSN in payer URL", "apps/agent_worker/x.py",
     'url = f"https://payer.example/members/{ssn}/eligibility"', "phi-url"),
    ("phi-url BLOCK: MRN in FastAPI route template", "apps/control_plane/x.py",
     '@router.get("/patients/{mrn}/calls")', "phi-url"),
    ("phi-url PASS: opaque token in URL", "apps/control_plane/x.py",
     'url = f"https://payer.example/members/{member_token}/eligibility"', None),
    ("phi-url PASS: opaque uuid in route", "apps/control_plane/x.py",
     '@router.get("/patients/{patient_id}/calls")', None),

    # ---- skips / overrides ------------------------------------------------------------
    ("skip: vendored phi_codec", "packages/phi_codec/phi_codec/vault/base.py",
     'logger.info(f"raw {raw_value}")', None),
    ("skip: CLAUDE.md doc", "vera-backend/CLAUDE.md",
     'NEVER do logger.info(f"...{ssn}...")', None),
    ("skip: test file", "tests/unit/test_x.py",
     'logger.info(f"patient {patient_name}")', None),
    ("override: # phi-guard: allow", "x.py",
     'logger.info(f"patient {patient_name}")  # phi-guard: allow', None),
]


def run_hook(payload: dict, extra_env: dict | None = None) -> int:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        text=True, capture_output=True, env=env,
    )
    return proc.returncode


def main() -> int:
    failures = 0

    for name, fp, text, expected in CASES:
        found = detected(fp, text)
        if expected is None:
            ok = not found
        else:
            ok = expected in found
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{status}] {name}" + ("" if ok else f"   (expected={expected!r} found={found})"))

    # End-to-end: prove the stdin -> exit-code contract the harness relies on.
    block = {"tool_name": "Write", "tool_input": {
        "file_path": "apps/x.py", "content": 'logger.info(f"patient {patient_name}")'}}
    allow = {"tool_name": "Write", "tool_input": {
        "file_path": "apps/x.py", "content": 'logger.info("ok %s", session_id)'}}

    e2e = [
        ("e2e BLOCK -> exit 2", run_hook(block) == 2),
        ("e2e PASS  -> exit 0", run_hook(allow) == 0),
        ("e2e VERA_PHI_GUARD=off -> exit 0", run_hook(block, {"VERA_PHI_GUARD": "off"}) == 0),
        ("e2e VERA_PHI_GUARD_SKIP=phi-logging -> exit 0",
         run_hook(block, {"VERA_PHI_GUARD_SKIP": "phi-logging"}) == 0),
    ]
    for name, ok in e2e:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{status}] {name}")

    total = len(CASES) + len(e2e)
    print(f"\n{total - failures}/{total} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
