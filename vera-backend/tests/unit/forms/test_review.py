"""Pure-logic tests for the IBV review/dispute helpers (no DB)."""

from uuid import uuid4

from vera_core.forms.review import (
    AnswerRow,
    EvalRow,
    adjudication_action,
    all_required_paths,
    build_field_views,
    completion_pct,
    is_disputed,
    unwrap_value,
)
from vera_core.models.enums import DisputeActionType

SCHEMA = {
    "sections": [
        {
            "section_key": "patient_information",
            "properties": {
                "patient_name": {"required_state": "required"},
                "patient_dob": {"required_state": "required"},
                "chart_number": {},
            },
        },
        {
            "section_key": "insurance_information",
            "properties": {"policy_number": {"required_state": "required"}},
        },
    ]
}


class TestUnwrapValue:
    def test_unwraps_value_wrapper(self) -> None:
        assert unwrap_value({"value": "Jane"}) == "Jane"

    def test_passes_through_non_wrapper(self) -> None:
        assert unwrap_value("Jane") == "Jane"
        assert unwrap_value({"a": 1}) == {"a": 1}
        assert unwrap_value(None) is None


class TestIsDisputed:
    def test_none_evaluation_not_disputed(self) -> None:
        assert is_disputed(None) is False

    def test_unsupported_is_disputed(self) -> None:
        assert is_disputed(EvalRow(supported=False, confidence=80, evidence="x")) is True

    def test_supported_not_disputed(self) -> None:
        assert is_disputed(EvalRow(supported=True, confidence=95, evidence=None)) is False

    def test_low_confidence_disputed_when_threshold_set(self) -> None:
        ev = EvalRow(supported=True, confidence=50, evidence=None)
        assert is_disputed(ev, min_confidence=70) is True
        assert is_disputed(ev) is False  # no threshold → only `supported` matters


class TestRequiredPathsAndCompletion:
    def test_all_required_paths(self) -> None:
        assert all_required_paths(SCHEMA) == [
            "insurance_information.policy_number",
            "patient_information.patient_dob",
            "patient_information.patient_name",
        ]

    def test_completion_pct(self) -> None:
        assert completion_pct(set(), SCHEMA) == 0.0
        assert completion_pct({"patient_information.patient_name"}, SCHEMA) == 33.33
        assert (
            completion_pct(
                {
                    "patient_information.patient_name",
                    "patient_information.patient_dob",
                    "insurance_information.policy_number",
                },
                SCHEMA,
            )
            == 100.0
        )

    def test_completion_pct_no_required_is_zero(self) -> None:
        assert completion_pct({"a.b"}, {"sections": []}) == 0.0


class TestAdjudicationAction:
    def test_accept_when_unchanged(self) -> None:
        assert adjudication_action("X", "X", set()) == DisputeActionType.ACCEPT.value

    def test_override_when_new_matches_a_prior(self) -> None:
        assert adjudication_action("OLD", "NEW", {"OLD"}) == DisputeActionType.OVERRIDE.value

    def test_correct_when_brand_new(self) -> None:
        assert adjudication_action("FRESH", "NEW", {"OLD"}) == DisputeActionType.CORRECT.value


class TestBuildFieldViews:
    def _answer(self, path: str, value: str, **kw: object) -> AnswerRow:
        return AnswerRow(
            id=uuid4(),
            field_path=path,
            value={"value": value},
            source=kw.get("source", "ai_call"),  # type: ignore[arg-type]
            confidence=kw.get("confidence"),  # type: ignore[arg-type]
            evidence=kw.get("evidence"),  # type: ignore[arg-type]
        )

    def test_disputed_and_undisputed_fields(self) -> None:
        a1 = self._answer(
            "insurance_information.health_plan", "Blue Cross", confidence=95, evidence="rep said so"
        )
        a2 = self._answer("patient_information.patient_name", "Jane", source="intake")
        evals = {a1.id: EvalRow(supported=False, confidence=72, evidence="judge: disagrees")}
        priors = {"insurance_information.health_plan": {"value": "BCBS TX"}}

        views = build_field_views([a1, a2], evals, priors, resolved_answer_ids=set())

        by_path = {v["field_path"]: v for v in views}
        d = by_path["insurance_information.health_plan"]["dispute"]
        assert d == {
            "previous_value": "BCBS TX",
            "current_value": "Blue Cross",
            "confidence": 72,
            "evidence": "rep said so",  # field_answer.evidence (what was captured)
            "reasoning": "judge: disagrees",  # field_evaluation.evidence (why disputed)
        }
        assert by_path["insurance_information.health_plan"]["value"] == "Blue Cross"
        assert by_path["patient_information.patient_name"]["dispute"] is None

    def test_resolved_answer_is_not_disputed(self) -> None:
        a1 = self._answer("insurance_information.health_plan", "Blue Cross")
        evals = {a1.id: EvalRow(supported=False, confidence=72, evidence="x")}
        views = build_field_views([a1], evals, {}, resolved_answer_ids={a1.id})
        assert views[0]["dispute"] is None

    def test_sorted_by_path(self) -> None:
        a = self._answer("b.x", "1")
        b = self._answer("a.y", "2")
        views = build_field_views([a, b], {}, {}, resolved_answer_ids=set())
        assert [v["field_path"] for v in views] == ["a.y", "b.x"]
