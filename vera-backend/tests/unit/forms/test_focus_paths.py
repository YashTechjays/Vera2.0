"""`is_call_confirmed` — did an AUTHORITATIVE call collect this, judge-supported?"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import (
    FieldStatus,
    _required_paths,
    focus_paths,
    is_call_confirmed,
    is_field_satisfied,
    retryable_required_paths,
)

AUTH, OTHER = uuid4(), uuid4()
CALLS = frozenset({AUTH})

_FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"


def _ibv() -> tuple[FormSchemaDoc, dict[str, Any]]:
    raw = json.loads((_FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text())
    return FormSchemaDoc.model_validate(raw), raw


def _family() -> dict[str, Any]:
    return {"sections.benefit_coverage.coverage_type": "Family"}


def _nothing_answered(raw: dict[str, Any]) -> dict[str, FieldStatus]:
    return {}


def _required_paths_for_asking(
    raw: dict[str, Any], values: dict[str, Any], *, include_defaulted: bool = True
) -> list[str]:
    return _required_paths(raw, values, askable_only=True, include_defaulted=include_defaulted)


def _status(
    source: str,
    *,
    supported: bool | None = True,
    confidence: int = 95,
    call_id: UUID = AUTH,
) -> FieldStatus:
    return FieldStatus(
        source=source, ai_supported=supported, ai_confidence=confidence, call_id=call_id
    )


class TestIsCallConfirmed:
    def test_authoritative_call_supported_answer_is_confirmed(self) -> None:
        assert is_call_confirmed(_status("ai_call"), authoritative_calls=CALLS, floor=70)

    def test_answer_from_a_non_authoritative_call_is_not(self) -> None:
        """The rep answered, but nothing ties the conversation to a payer record."""
        assert not is_call_confirmed(
            _status("ai_call", call_id=OTHER), authoritative_calls=CALLS, floor=70
        )

    def test_intake_value_is_not_confirmed_even_though_it_is_satisfied(self) -> None:
        """The divergence from `is_field_satisfied` that this whole predicate exists for."""
        intake = FieldStatus(source="intake", ai_supported=None, ai_confidence=None, call_id=None)
        assert is_field_satisfied(intake, floor=70) is True
        assert is_call_confirmed(intake, authoritative_calls=CALLS, floor=70) is False

    def test_human_value_is_not_confirmed(self) -> None:
        human = FieldStatus(source="human", ai_supported=None, ai_confidence=None, call_id=None)
        assert not is_call_confirmed(human, authoritative_calls=CALLS, floor=70)

    def test_judge_rejected_answer_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(
            _status("ai_call", supported=False, confidence=38), authoritative_calls=CALLS, floor=70
        )

    def test_below_floor_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(
            _status("ai_call", confidence=69), authoritative_calls=CALLS, floor=70
        )

    def test_unjudged_answer_is_not_confirmed(self) -> None:
        """No `field_evaluation` row yet — `ai_supported` is None, so nothing is proven."""
        assert not is_call_confirmed(
            _status("ai_call", supported=None), authoritative_calls=CALLS, floor=70
        )

    def test_absent_status_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(None, authoritative_calls=CALLS, floor=70)


class TestDefaultedLeavesInTheAskSet:
    """The retry ask set follows `owed_now`, not `completion_pct_v2`, on defaulted leaves."""

    def test_retryable_required_paths_still_excludes_them(self) -> None:
        """Unchanged: this predicate answers "is a retry WORTH placing", and a defaulted leaf the
        form calls done must not keep a form redialing (spec D3)."""
        _doc, raw = _ibv()
        owed = retryable_required_paths(_nothing_answered(raw), raw, floor=70, values=_family())
        assert "sections.patient_information.spouse_partner_name" not in owed

    def test_the_ask_set_includes_them(self) -> None:
        _doc, raw = _ibv()
        asked = _required_paths_for_asking(raw, _family())
        assert "sections.patient_information.spouse_partner_name" in asked
        assert "sections.patient_information.spouse_partner_dob" in asked

    def test_the_seven_family_plan_defaulted_leaves(self) -> None:
        """Measured on ibv_form_standard_v2: 40 askable required+applicable today, 47 with
        defaults. These seven are the ones a fresh call asks and a retry silently skips."""
        _doc, raw = _ibv()
        today = set(_required_paths_for_asking(raw, _family(), include_defaulted=False))
        with_defaults = set(_required_paths_for_asking(raw, _family(), include_defaulted=True))
        assert with_defaults - today == {
            "sections.patient_information.spouse_partner_name",
            "sections.patient_information.spouse_partner_dob",
            "sections.insurance_information.group_name",
            "sections.insurance_information.group_number",
            "sections.insurance_information.policy_situs",
            "sections.benefit_coverage.telehealth_covered",
            "sections.enrollment.enrollment_required",
        }


class TestFocusPaths:
    def test_an_authoritatively_confirmed_field_is_not_asked_again(self) -> None:
        doc, raw = _ibv()
        target = "sections.insurance_information.plan_type"
        status = {target: FieldStatus("ai_call", True, 95, AUTH)}
        paths = focus_paths(
            doc, status, raw, floor=70, values={target: "PPO"}, authoritative_calls={AUTH}
        )
        assert target not in paths

    def test_the_same_field_from_a_non_authoritative_call_IS_asked_again(self) -> None:
        doc, raw = _ibv()
        target = "sections.insurance_information.plan_type"
        status = {target: FieldStatus("ai_call", True, 95, OTHER)}
        paths = focus_paths(
            doc, status, raw, floor=70, values={target: "PPO"}, authoritative_calls={AUTH}
        )
        assert target in paths

    def test_an_intake_value_is_asked_because_no_call_confirmed_it(self) -> None:
        doc, raw = _ibv()
        target = "sections.insurance_information.group_name"
        status = {target: FieldStatus("intake", None, None, None)}
        paths = focus_paths(
            doc, status, raw, floor=70, values={target: "Umbrella"}, authoritative_calls={AUTH}
        )
        assert target in paths

    def test_call_scoped_paths_are_always_present(self) -> None:
        """Even fully confirmed by an authoritative call: they describe THIS call (Plan A)."""
        doc, raw = _ibv()
        ref = doc.rep_call_reference_number_field
        status = {p: FieldStatus("ai_call", True, 95, AUTH) for p in doc.collected_per_call_paths()}
        paths = focus_paths(
            doc,
            status,
            raw,
            floor=70,
            values=dict.fromkeys(status, "x"),
            authoritative_calls={AUTH},
        )
        assert doc.collected_per_call_paths() <= set(paths)
        assert ref in paths

    def test_one_missing_group_member_pulls_in_its_whole_panel(self) -> None:
        """`expand_to_groups`: a partial panel reads oddly on a call.

        `copay` is itself gated on its group's `covered` field and the section's
        `diagnostic_testing_covered` ref, so both must be on file for `copay` to be
        applicable at all before its lone-gap status can pull in the panel."""
        doc, raw = _ibv()
        target = "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.copay"
        status = {target: FieldStatus("ai_call", False, 38, AUTH)}
        values = {
            "sections.diagnostic_testing.diagnostic_testing_covered": "Yes",
            "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.covered": "Yes",
            target: "$25",
        }
        paths = set(
            focus_paths(doc, status, raw, floor=70, values=values, authoritative_calls={AUTH})
        )
        panel = "sections.diagnostic_testing.labs_xray_ultrasound."
        assert len([p for p in paths if p.startswith(panel)]) == 32

    def test_returns_document_order_without_duplicates(self) -> None:
        doc, raw = _ibv()
        paths = focus_paths(doc, {}, raw, floor=70, values={}, authoritative_calls=set())
        assert len(paths) == len(set(paths))
        order = doc.collection_paths()
        ranked = [order.index(p) for p in paths if p in order]
        assert ranked == sorted(ranked)
