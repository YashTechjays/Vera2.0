"""Pure-logic tests for the IBV review/dispute helpers (no DB)."""

import json
from typing import Any
from uuid import UUID, uuid4

from vera_core.forms.conditions import is_applicable, is_required, leaf_gates
from vera_core.forms.dsl import COLLECTED_ROLES, FormSchemaDoc, load_document
from vera_core.forms.review import (
    AnswerRow,
    FieldStatus,
    adjudication_action,
    all_required_paths,
    build_field_views,
    completion_pct,
    completion_pct_v2,
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
from .test_schema_dsl import minimal_doc

_IBV_DOC: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)

# Real IBV paths for the call-scoped-dispute tests below: REP_NAME/REF sit in the
# `insurance_representative` section (`collected_per: "call"`); COPAY is an ordinary
# form-scoped leaf.
REP_NAME = "sections.insurance_representative.rep_name"
REF = _IBV_DOC.rep_call_reference_number_field
COPAY = "sections.infertility_treatment.ovulation_induction.copay"


def _ibv_raw() -> dict[str, Any]:
    text = (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
    raw: dict[str, Any] = json.loads(text)
    return raw


def _context_only_doc() -> dict[str, Any]:
    """A minimal doc whose only required leaf has role="context" and no prompt — it can
    never be filled BY A CALL, so a correct completion reading over it is vacuous."""
    doc = minimal_doc()
    doc["sections"]["basics"]["fields"] = {
        "plan_type": {"type": "text", "title": "Plan Type", "role": "context", "required": True}
    }
    return doc


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


class TestCompletionCountsOnlyCollectableLeaves:
    """A call can only fill `ask`/`confirm` leaves, so the rest do not measure a call's progress.

    Worse than inert: every non-askable required leaf in `ibv_form_standard_v2` is also a
    `required_intake_fields` target, so `missing_required` blocks form creation without them and
    they are ALWAYS filled — a constant offset that no call could move. And `post_call.py:93`
    gates a retry on this number.
    """

    def test_a_form_with_only_intake_context_reads_near_zero(self) -> None:
        """Not literally 0.0: 5 of the 39 relevant askable leaves in ibv_form_standard_v2
        declare a `default` ("N/A"), and `is_satisfied` counts a declared default as filled
        regardless of the call (spec §4.4, same rule the export applies) — pre-existing
        behavior this fix does not touch. The other 34 correctly read as unfilled."""
        raw = _ibv_raw()
        doc = FormSchemaDoc.model_validate(raw)
        values = {path: "x" for path, leaf in doc.leaf_items() if leaf.role not in COLLECTED_ROLES}
        shared = doc.shared_conditions or {}
        relevant = [
            leaf
            for _path, leaf, gates in leaf_gates(doc)
            if leaf.role in COLLECTED_ROLES
            and is_applicable(gates, values, shared)
            and is_required(leaf, values, shared)
        ]
        defaulted = sum(1 for leaf in relevant if leaf.default is not None)
        assert completion_pct_v2(values, raw) == round(defaulted / len(relevant) * 100, 2)

    def test_context_leaves_do_not_dilute_a_collected_answer(self) -> None:
        """Filling one askable leaf moves completion by exactly 1/(askable denominator) —
        isolated from the constant a declared default contributes (identical on both
        sides), so this isolates the fix rather than that separate, pre-existing rule."""
        raw = _ibv_raw()
        doc = FormSchemaDoc.model_validate(raw)
        context = {p: "x" for p, leaf in doc.leaf_items() if leaf.role not in COLLECTED_ROLES}
        target = "sections.patient_verification.is_insurance_active"
        without_target = completion_pct_v2(context, raw)
        with_target = completion_pct_v2({**context, target: "Yes"}, raw)
        shared = doc.shared_conditions or {}
        askable_denominator = len(
            [
                p
                for p, leaf, gates in leaf_gates(doc)
                if leaf.role in COLLECTED_ROLES
                and is_applicable(gates, {**context, target: "Yes"}, shared)
                and is_required(leaf, {**context, target: "Yes"}, shared)
            ]
        )
        assert round(with_target - without_target, 2) == round(1 / askable_denominator * 100, 2)

    def test_a_schema_with_no_askable_required_leaves_is_complete(self) -> None:
        """The `if not relevant: return 100.0` branch still holds when the filter empties it —
        a real semantic change: a form whose only required leaf is context-only now reads
        100% complete rather than 0%, because no call can ever move that leaf."""
        raw = _context_only_doc()
        assert completion_pct_v2({}, raw) == 100.0


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


class TestCallScopedPathsAreNeverDisputed:
    """A call-scoped answer has no form-level baseline by definition, so it cannot diverge from
    one. Today the rep's name and the call reference number are flagged on EVERY call with
    `previous_value: null` — 152 such views on the seeded form."""

    def _rows(self) -> list[AnswerRow]:
        return [
            AnswerRow(uuid4(), REP_NAME, {"value": "Priya Raman"}, "ai_call", 90, None),
            AnswerRow(uuid4(), REF, {"value": "9310-KT-04"}, "ai_call", 90, None),
            AnswerRow(uuid4(), COPAY, {"value": "$25"}, "ai_call", 90, None),
        ]

    def test_a_call_scoped_path_is_not_disputed(self) -> None:
        views = {
            v["field_path"]: v
            for v in build_field_views(self._rows(), {}, call_scoped_paths={REP_NAME, REF})
        }
        assert views[REP_NAME]["dispute"] is None
        assert views[REF]["dispute"] is None

    def test_a_form_scoped_path_is_still_disputed(self) -> None:
        """The global rule is untouched — only the declared paths are exempt."""
        views = {
            v["field_path"]: v
            for v in build_field_views(self._rows(), {}, call_scoped_paths={REP_NAME, REF})
        }
        assert views[COPAY]["dispute"] is not None
        assert views[COPAY]["dispute"]["previous_value"] is None

    def test_evidence_survives_suppression(self) -> None:
        """`evidence` is top-level precisely because an answer with no dispute still has evidence
        worth reviewing — suppressing the dispute must not hide it."""
        rows = [AnswerRow(uuid4(), REF, {"value": "R"}, "ai_call", 90, "the rep read it back")]
        [view] = build_field_views(rows, {}, call_scoped_paths={REF})
        assert view["dispute"] is None
        assert view["evidence"] == "the rep read it back"

    def test_default_is_unchanged_behaviour(self) -> None:
        """Callers that pass nothing keep today's semantics exactly."""
        views = build_field_views(self._rows(), {})
        assert all(v["dispute"] is not None for v in views)


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
    """Pre-Plan-B, `_status()`'s default (ai_call, no call_id) was itself sufficient to
    satisfy — these tests now pass an explicit authoritative call so they still exercise
    the fraction arithmetic rather than trivially reading 0.0 for lack of one."""

    def test_none_satisfied_is_zero(self) -> None:
        # No status at all — unaffected by the authoritative-call rule either way.
        assert satisfied_required_fraction({}, SCHEMA, floor=70, authoritative_calls=set()) == 0.0

    def test_one_of_three_satisfied(self) -> None:
        call_id = uuid4()
        assert (
            satisfied_required_fraction(
                {"patient_information.patient_name": _status(call_id=call_id)},
                SCHEMA,
                floor=70,
                authoritative_calls={call_id},
            )
            == 1 / 3
        )

    def test_all_satisfied_is_one(self) -> None:
        call_id = uuid4()
        status = {
            "patient_information.patient_name": _status(call_id=call_id),
            "patient_information.patient_dob": _status(call_id=call_id),
            "insurance_information.policy_number": _status(call_id=call_id),
        }
        assert (
            satisfied_required_fraction(status, SCHEMA, floor=70, authoritative_calls={call_id})
            == 1.0
        )

    def test_no_applicable_required_is_one(self) -> None:
        # No required fields in the schema at all — vacuous, regardless of authority.
        assert (
            satisfied_required_fraction({}, {"sections": []}, floor=70, authoritative_calls=set())
            == 1.0
        )

    def test_ai_answer_below_floor_counts_unsatisfied(self) -> None:
        """Confidence still gates even on an authoritative call — isolating the floor
        check from the authority check, which `TestVerifiedCountsOnlyAuthoritativeAnswers`
        covers separately."""
        call_id = uuid4()
        weak = FieldStatus(source="ai_call", ai_supported=True, ai_confidence=50, call_id=call_id)
        assert (
            satisfied_required_fraction(
                {"patient_information.patient_name": weak},
                SCHEMA,
                floor=70,
                authoritative_calls={call_id},
            )
            == 0.0
        )


class TestVerifiedCountsOnlyAuthoritativeAnswers:
    def test_intake_values_are_not_verified(self) -> None:
        """The headline defect: a form nobody has called reported 100% verified and routed to
        READY_FOR_REVIEW — "nothing is wrong, sign it off" — with zero judge verdicts in existence.
        """
        raw = _ibv_raw()
        doc = FormSchemaDoc.model_validate(raw)
        status: dict[str, FieldStatus] = {}
        values: dict[str, Any] = {}
        for path, _leaf, gates in leaf_gates(doc):
            if is_applicable(gates, values, doc.shared_conditions or {}):
                status[path] = FieldStatus("intake", None, None, None)
                values[path] = "x"
        assert (
            satisfied_required_fraction(
                status, raw, floor=70, values=values, authoritative_calls=set()
            )
            == 0.0
        )

    def test_an_answer_from_a_non_authoritative_call_is_not_verified(self) -> None:
        raw, auth, other = _ibv_raw(), uuid4(), uuid4()
        target = "sections.patient_verification.is_insurance_active"
        status = {target: FieldStatus("ai_call", True, 95, other)}
        frac = satisfied_required_fraction(
            status, raw, floor=70, values={target: "Yes"}, authoritative_calls={auth}
        )
        assert frac == 0.0

    def test_the_same_answer_from_an_authoritative_call_is(self) -> None:
        raw, auth = _ibv_raw(), uuid4()
        target = "sections.patient_verification.is_insurance_active"
        status = {target: FieldStatus("ai_call", True, 95, auth)}
        frac = satisfied_required_fraction(
            status, raw, floor=70, values={target: "Yes"}, authoritative_calls={auth}
        )
        assert frac > 0.0

    def test_one_hundred_percent_stays_reachable(self) -> None:
        """The reason the denominator must shrink too: with `askable_only=False` the
        never-collectable leaves would stay in the divisor while becoming permanently
        unsatisfiable, capping this below 100% — so a `retry_fill_threshold` above that
        cap could never fire the park gate."""
        raw, auth = _ibv_raw(), uuid4()
        doc = FormSchemaDoc.model_validate(raw)
        status: dict[str, FieldStatus] = {}
        values: dict[str, Any] = {}
        for path, leaf, _gates in leaf_gates(doc):
            if leaf.role in COLLECTED_ROLES:
                status[path] = FieldStatus("ai_call", True, 95, auth)
                values[path] = "Yes"
        assert (
            satisfied_required_fraction(
                status, raw, floor=70, values=values, authoritative_calls={auth}
            )
            == 1.0
        )
