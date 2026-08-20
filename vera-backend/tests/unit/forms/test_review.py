"""Pure-logic tests for the IBV review/dispute helpers (no DB)."""

from uuid import UUID, uuid4

from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.review import (
    AnswerRow,
    FieldStatus,
    adjudication_action,
    all_required_paths,
    build_field_views,
    completion_pct,
    dispute_view,
    expand_to_groups,
    has_call_reference,
    is_disputed,
    normalize_value,
    satisfied_required_fraction,
    unwrap_value,
)
from vera_core.models.enums import DisputeActionType

from .test_prompting import FORM_SCHEMA_DIR

_IBV_DOC: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)

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


class TestDisputeView:
    """dispute_view is the shared payload builder \u2014 the live SSE path and the detail view
    both go through it, so its dict shape is what the UI renders either way."""

    def test_diverging_ai_value_builds_payload(self) -> None:
        # No `evidence` key: it belongs to the answer, not the divergence, so it rides on
        # the field view instead (an agreeing AI answer has evidence but no dispute).
        assert dispute_view(
            source="ai_call",
            value={"value": "Blue Cross"},
            confidence=88,
            baseline_value={"value": "BCBS TX"},
        ) == {
            "previous_value": "BCBS TX",
            "current_value": "Blue Cross",
            "confidence": 88,
            "reasoning": None,
        }

    def test_matching_baseline_is_none(self) -> None:
        assert (
            dispute_view(
                source="ai_call",
                value="BCBS TX",
                confidence=None,
                baseline_value={"value": "BCBS TX"},
            )
            is None
        )

    def test_absent_baseline_disputes_non_null_value(self) -> None:
        view = dispute_view(source="ai_call", value="Aetna", confidence=None, baseline_value=None)
        assert view is not None
        assert view["previous_value"] is None
        assert view["current_value"] == "Aetna"

    def test_non_ai_source_is_never_disputed(self) -> None:
        assert dispute_view(source="human", value="x", confidence=None, baseline_value=None) is None

    def test_accepts_raw_unwrapped_values(self) -> None:
        # The live path passes raw values (not {"value": ...}) \u2014 must behave identically.
        assert dispute_view(source="ai_call", value="Yes", confidence=90, baseline_value="No") == {
            "previous_value": "No",
            "current_value": "Yes",
            "confidence": 90,
            "reasoning": None,
        }


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
            "reasoning": None,  # field_evaluation plays no part in disputes
        }
        # Evidence is top-level on the view, never nested in the dispute.
        assert by_path["insurance_information.health_plan"]["evidence"] == "rep said so"
        assert by_path["insurance_information.health_plan"]["value"] == "Blue Cross"
        assert by_path["patient_information.patient_name"]["dispute"] is None

    def test_ai_value_matching_baseline_is_not_disputed(self) -> None:
        a = self._answer("insurance_information.health_plan", "X", confidence=80)
        views = build_field_views([a], {"insurance_information.health_plan": {"value": "X"}})
        assert views[0]["dispute"] is None

    def test_evidence_is_top_level_even_when_undisputed(self) -> None:
        """The reason evidence is not dispute-nested: an AI answer that AGREES with the
        baseline has no dispute, but its evidence is still what a reviewer needs."""
        a = self._answer(
            "insurance_information.health_plan", "X", confidence=80, evidence="rep said X"
        )
        views = build_field_views([a], {"insurance_information.health_plan": {"value": "X"}})
        assert views[0]["dispute"] is None
        assert views[0]["evidence"] == "rep said X"

    def test_evidence_is_none_when_the_answer_carries_none(self) -> None:
        # The live Observer path stamps evidence_seq but never the text column.
        a = self._answer("insurance_information.health_plan", "New", confidence=80)
        assert build_field_views([a], {})[0]["evidence"] is None

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


def _status(source: str = "ai_call", *, call_id: UUID | None = None) -> FieldStatus:
    return FieldStatus(source=source, ai_supported=True, ai_confidence=90, call_id=call_id)


class TestHasCallReference:
    """The retry-scope gate: a reference number captured BY A CALL → FOCUSED retry."""

    def test_true_when_a_call_captured_the_reference(self) -> None:
        ref = _IBV_DOC.rep_call_reference_number_field
        assert has_call_reference({ref: _status(call_id=uuid4())}, _IBV_DOC) is True

    def test_false_when_reference_answer_absent(self) -> None:
        assert has_call_reference({}, _IBV_DOC) is False

    def test_false_when_a_human_typed_the_reference_without_a_call(self) -> None:
        """An operator can type a reference number into the form for a form that was never
        dialed (`source=human`, no `call_id`) — that must not open the focused (required-only)
        set on what would otherwise be its first call (spec D8)."""
        ref = _IBV_DOC.rep_call_reference_number_field
        assert has_call_reference({ref: _status(source="human")}, _IBV_DOC) is False


class TestExpandToGroups:
    """A missing field inside a group pulls in every collectable leaf of that group."""

    @staticmethod
    def _group_with_multiple_leaves() -> tuple[str, list[str]] | None:
        collectable = _IBV_DOC.collection_paths()
        for group_path in _IBV_DOC.group_paths():
            prefix = f"{group_path}."
            leaves = [p for p in collectable if p.startswith(prefix)]
            if len(leaves) > 1:
                return group_path, leaves
        return None

    def test_grouped_field_expands_to_whole_group(self) -> None:
        found = self._group_with_multiple_leaves()
        assert found is not None, "IBV schema is expected to contain a multi-leaf group"
        _, leaves = found
        expanded = expand_to_groups(_IBV_DOC, {leaves[0]})
        assert set(leaves).issubset(set(expanded))

    def test_result_is_document_ordered(self) -> None:
        found = self._group_with_multiple_leaves()
        assert found is not None
        _, leaves = found
        expanded = expand_to_groups(_IBV_DOC, {leaves[-1]})
        collectable = _IBV_DOC.collection_paths()
        assert expanded == [p for p in collectable if p in set(expanded)]

    def test_ungrouped_field_passes_through(self) -> None:
        # The call-reference leaf sits directly under a section, in no group.
        ref = _IBV_DOC.rep_call_reference_number_field
        assert expand_to_groups(_IBV_DOC, {ref}) == [ref]


class TestSatisfiedRequiredFraction:
    def test_none_satisfied_is_zero(self) -> None:
        assert satisfied_required_fraction({}, SCHEMA, floor=70) == 0.0

    def test_one_of_three_satisfied(self) -> None:
        assert (
            satisfied_required_fraction(
                {"patient_information.patient_name": _status()}, SCHEMA, floor=70
            )
            == 1 / 3
        )

    def test_all_satisfied_is_one(self) -> None:
        status = {
            "patient_information.patient_name": _status(),
            "patient_information.patient_dob": _status(),
            "insurance_information.policy_number": _status(),
        }
        assert satisfied_required_fraction(status, SCHEMA, floor=70) == 1.0

    def test_no_applicable_required_is_one(self) -> None:
        assert satisfied_required_fraction({}, {"sections": []}, floor=70) == 1.0

    def test_ai_answer_below_floor_counts_unsatisfied(self) -> None:
        weak = FieldStatus(source="ai_call", ai_supported=True, ai_confidence=50)
        assert (
            satisfied_required_fraction(
                {"patient_information.patient_name": weak}, SCHEMA, floor=70
            )
            == 0.0
        )
