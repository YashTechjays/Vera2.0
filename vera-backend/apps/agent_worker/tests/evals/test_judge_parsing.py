"""Deterministic tests for the evaluator's plumbing — parsing and citation checks.

Deliberately NOT marked `evals`: these need no LLM and no database, so they run in `just check`.
Only the judgement itself is nondeterministic; the machinery around it should not be.
"""

from judge import DIMENSIONS, Finding, Report, _parse, verify_citations


def test_parses_a_plain_json_array() -> None:
    findings = _parse('[{"dimension": "tool_calls", "verdict": "pass", "reason": "ok", "turn": 3}]')
    assert findings == [Finding(dimension="tool_calls", verdict="pass", reason="ok", turn=3)]


def test_tolerates_a_code_fence() -> None:
    # The prompt forbids a fence; models add one anyway, and losing a whole evaluation to
    # formatting would be worse than accepting it.
    reply = (
        '```json\n[{"dimension": "closing", "verdict": "fail", "reason": "twice", "turn": 9}]\n```'
    )
    assert [f.dimension for f in _parse(reply)] == ["closing"]


def test_unparseable_reply_yields_no_findings() -> None:
    assert _parse("I think the call went well, honestly.") == []


def test_drops_unknown_dimensions_and_verdicts() -> None:
    # A hallucinated dimension or a freeform verdict must not reach the report.
    reply = (
        '[{"dimension": "vibes", "verdict": "pass", "reason": "", "turn": 1},'
        ' {"dimension": "closing", "verdict": "excellent", "reason": "", "turn": 1}]'
    )
    assert _parse(reply) == []


def test_null_turn_is_allowed() -> None:
    # Some dimensions genuinely apply to the call as a whole rather than one line.
    findings = _parse(
        '[{"dimension": "overall", "verdict": "pass", "reason": "fine", "turn": null}]'
    )
    assert findings[0].turn is None


def test_citation_outside_the_transcript_is_discarded() -> None:
    findings = [
        Finding(dimension="closing", verdict="fail", reason="real", turn=4),
        Finding(dimension="overall", verdict="fail", reason="invented", turn=99),
    ]
    kept, discarded = verify_citations(findings, line_count=10)
    assert [f.reason for f in kept] == ["real"]
    assert discarded == 1


def test_a_null_citation_survives_verification() -> None:
    kept, discarded = verify_citations([Finding("overall", "pass", "fine", None)], line_count=3)
    assert len(kept) == 1 and discarded == 0


def test_failures_lists_only_fails() -> None:
    report = Report(
        findings=[
            Finding("closing", "pass", "", None),
            Finding("tool_calls", "fail", "bad", 2),
            Finding("gap_conduct", "n/a", "", None),
        ]
    )
    assert [f.dimension for f in report.failures] == ["tool_calls"]


def test_render_marks_failures_and_notes_discards() -> None:
    report = Report(findings=[Finding("closing", "fail", "signed off twice", 12)], discarded=2)
    rendered = report.render("some scenario")
    assert "closing" in rendered and "FAIL" in rendered and "[12]" in rendered
    assert "2 discarded" in rendered


def test_every_dimension_has_a_question() -> None:
    # The prompt is built from this mapping, so an empty entry would silently ask nothing.
    assert all(question.strip() for question in DIMENSIONS.values())
