"""Recall gate: detection must not leak PHI on synthetic spoken-form data.

Redaction recall is the compliance metric (a miss == PHI to the LLM). We assert a
high floor and zero leak-canary trips; type recall is checked more loosely since
mis-typing (still redacted) is not a leak.
"""

import pytest

from phi_codec.eval.recall import run

pytestmark = pytest.mark.asyncio


async def test_redaction_recall_and_no_leaks():
    report = await run(150, seed=7, use_gliner=False)

    total = sum(s.total for s in report["stats"].values())
    redacted = sum(s.redacted for s in report["stats"].values())
    overall = redacted / total

    assert overall >= 0.99, f"redaction recall {overall:.3%} below floor"
    assert report["leak_turns"] == 0, "leak canary tripped — PHI shape survived tokenization"

    # Every structured-ID type must be fully redacted (these are the high-risk ones).
    for etype in ("SSN", "BENEFICIARY_ID", "MBI", "PHONE"):
        st = report["stats"].get(etype)
        if st:
            assert st.redaction_recall == 1.0, f"{etype} redaction recall {st.redaction_recall:.3%}"


async def test_latency_budget_regex_path():
    report = await run(150, seed=7, use_gliner=False)
    # Regex+spaCy path must stay well under the 80ms tokenize budget.
    assert report["latency_p95"] < 80.0, f"p95 {report['latency_p95']:.1f}ms exceeds budget"
