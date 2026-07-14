"""Tests for `build_agent_context` — the pure {{token}} -> value resolver used at dispatch
(vera_core.services.ivr_selection). Uses the real ibv_standard schema + an in-memory
`{field_path: value}` map (no DB), mirroring the active-field_answer map the async wrapper builds.
"""

from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.services.ivr_selection import build_agent_context

_DOC = build_ibv_standard()

# field paths (schema system_fields targets) → their active value.
_MEMBER = "sections.insurance_information.policy_number"
_NAME = "sections.patient_information.patient_name"
_DOB = "sections.patient_information.patient_dob"
_NPI = "sections.provider_reference_information.npi"
_TAX = "sections.hospital_information.tax_id"
_GROUP = "sections.insurance_information.group_number"


def test_resolves_handles_from_active_values() -> None:
    context = build_agent_context(
        _DOC,
        {
            _MEMBER: "POL-661522",
            _NAME: "jane roe",
            _DOB: "1990-03-07",  # ISO stored → normalized to MM/DD/YYYY
            _NPI: "9998887776",
            _TAX: "112223333",
            _GROUP: "N/A",  # dropped
        },
    )
    assert context["member_id"] == "POL-661522"
    assert context["patient_name"] == "jane roe"
    assert context["patient_dob"] == "03/07/1990"  # date leaf normalized for speech
    assert context["doctor_npi"] == "9998887776"
    assert context["hospital_tax_id"] == "112223333"
    # group_number is a collected ask leaf (no handle / not context) → never resolved
    assert "group_number" not in context
    # nothing empty leaks in
    assert all(v for v in context.values())


def test_na_and_missing_values_are_dropped() -> None:
    context = build_agent_context(_DOC, {_MEMBER: "M1", _NAME: "  ", _TAX: "N/A"})
    assert context["member_id"] == "M1"
    assert "patient_name" not in context  # blank dropped
    assert "hospital_tax_id" not in context  # "N/A" dropped


def test_no_values_yields_empty_context() -> None:
    assert build_agent_context(_DOC, {}) == {}
