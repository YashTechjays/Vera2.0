"""Pure-logic tests for the IBV review/dispute helpers (no DB)."""

from uuid import uuid4

from vera_core.forms.review import (
    AnswerRow,
    adjudication_action,
    all_required_paths,
    build_field_views,
    completion_pct,
    is_disputed,
    normalize_value,
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


class TestNormalizeValue:
    def test_strings_are_trimmed_and_lowercased(self) -> None:
        assert normalize_value("  Primary ") == "primary"
        assert normalize_value("SECONDARY") == "secondary"

    def test_non_strings_pass_through(self) -> None:
        assert normalize_value(None) is None
        assert normalize_value(5) == 5
        assert normalize_value(True) is True
        assert normalize_value({"a": 1}) == {"a": 1}

    def test_only_ascii_whitespace_is_trimmed(self) -> None:
        # A non-breaking space (U+00A0) is NOT ASCII whitespace, so it is retained — it
        # stays a real value difference (a deliberately conservative, stable rule).
        assert normalize_value("\tPrimary\n") == "primary"
        assert normalize_value("\u00a0Primary") == "\u00a0primary"


class TestIsDisputed:
    def _answer(self, value: object, source: str = "ai_call") -> AnswerRow:
        return AnswerRow(
            id=uuid4(),
            field_path="insurance_information.health_plan",
            value={"value": value},
            source=source,
            confidence=None,
            evidence=None,
        )

    def test_ai_value_diverging_from_baseline_is_disputed(self) -> None:
        assert is_disputed(self._answer("Blue Cross"), {"value": "BCBS TX"}) is True

    def test_ai_value_equal_to_baseline_not_disputed(self) -> None:
        assert is_disputed(self._answer("BCBS TX"), {"value": "BCBS TX"}) is False

    def test_intake_or_human_current_never_disputed(self) -> None:
        # The current row IS the baseline → never a dispute, however the value compares.
        assert (
            is_disputed(self._answer("Blue Cross", source="intake"), {"value": "BCBS TX"}) is False
        )
        assert is_disputed(self._answer("X", source="human"), None) is False

    def test_ai_value_with_no_baseline_is_disputed(self) -> None:
        # Absent baseline is NULL → IS DISTINCT FROM a non-null AI value → disputed.
        assert is_disputed(self._answer("Blue Cross"), None) is True

    def test_null_value_and_no_baseline_not_disputed(self) -> None:
        assert is_disputed(self._answer(None), None) is False

    def test_ai_value_diverging_from_explicit_null_baseline_is_disputed(self) -> None:
        # Intake explicitly set null ({"value": None}) → AI value diverges → disputed.
        assert is_disputed(self._answer("Tertiary"), {"value": None}) is True

    def test_ai_null_diverging_from_value_baseline_is_disputed(self) -> None:
        # AI cleared a field the baseline had set → null IS DISTINCT FROM value → disputed.
        assert is_disputed(self._answer(None), {"value": "Primary"}) is True

    def test_case_only_difference_is_not_disputed(self) -> None:
        assert is_disputed(self._answer("primary"), {"value": "Primary"}) is False

    def test_whitespace_only_difference_is_not_disputed(self) -> None:
        assert is_disputed(self._answer(" Primary "), {"value": "Primary"}) is False

    def test_case_and_whitespace_difference_is_not_disputed(self) -> None:
        assert is_disputed(self._answer("  PRIMARY  "), {"value": "primary"}) is False

    def test_genuinely_different_value_still_disputed(self) -> None:
        assert is_disputed(self._answer("Secondary"), {"value": "Primary"}) is True

    def test_non_ascii_whitespace_difference_is_disputed(self) -> None:
        # A non-breaking space (U+00A0) is not ASCII whitespace, so it is NOT stripped —
        # this stays a dispute.
        assert is_disputed(self._answer("\u00a0Primary"), {"value": "Primary"}) is True


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
        baselines = {"insurance_information.health_plan": {"value": "BCBS TX"}}

        views = build_field_views([a1, a2], baselines)

        by_path = {v["field_path"]: v for v in views}
        d = by_path["insurance_information.health_plan"]["dispute"]
        assert d == {
            "previous_value": "BCBS TX",  # the intake/human baseline
            "current_value": "Blue Cross",  # the diverging AI value
            "confidence": 95,  # the AI answer's own confidence
            "evidence": "rep said so",  # field_answer.evidence (what was captured)
            "reasoning": None,  # field_evaluation plays no part in disputes
        }
        assert by_path["insurance_information.health_plan"]["value"] == "Blue Cross"
        assert by_path["patient_information.patient_name"]["dispute"] is None

    def test_ai_value_matching_baseline_is_not_disputed(self) -> None:
        a = self._answer("insurance_information.health_plan", "X", confidence=80)
        views = build_field_views([a], {"insurance_information.health_plan": {"value": "X"}})
        assert views[0]["dispute"] is None

    def test_ai_value_without_baseline_is_disputed(self) -> None:
        a = self._answer("insurance_information.health_plan", "New", confidence=80)
        d = build_field_views([a], {})[0]["dispute"]
        assert d is not None
        assert d["previous_value"] is None
        assert d["current_value"] == "New"

    def test_sorted_by_path(self) -> None:
        a = self._answer("b.x", "1")
        b = self._answer("a.y", "2")
        views = build_field_views([a, b], {})
        assert [v["field_path"] for v in views] == ["a.y", "b.x"]
