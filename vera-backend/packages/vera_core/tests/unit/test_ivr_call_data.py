"""Tests for `add_ivr_call_data_metadata` (vera_core.services.ivr_selection).

The helper reads patient/provider identifiers off an already-loaded PatientForm (no DB) and
attaches them to dispatch metadata under `ivr_call_data`. These tests construct in-memory
PatientForm rows (no session/flush) and assert the extracted metadata.
"""

from datetime import date

from vera_core.models import PatientForm
from vera_core.schemas import IvrCallData
from vera_core.services.ivr_selection import add_ivr_call_data_metadata


def test_extracts_promoted_columns_and_payload_paths() -> None:
    form = PatientForm(
        patient_name="jane roe",
        member_id="ZZZ123",
        patient_dob=date(1990, 3, 7),
        intake_payload={
            "insurance_information": {"group_number": "GRP42"},
            "provider_reference_information": {"npi": "9998887776"},
            "hospital_information": {"tax_id": "112223333"},
        },
    )
    metadata: dict = {}
    add_ivr_call_data_metadata(form, metadata)
    assert metadata["ivr_call_data"] == {
        "patient_name": "jane roe",
        "member_id": "ZZZ123",
        "date_of_birth": "03/07/1990",  # MM/DD/YYYY
        "group_number": "GRP42",
        "provider_npi": "9998887776",
        "provider_id": "9998887776",  # reuses the NPI (no distinct source)
        "tax_id": "112223333",
    }


def test_drops_na_and_missing_values() -> None:
    form = PatientForm(
        patient_name=None,
        member_id="M1",
        patient_dob=None,
        intake_payload={"insurance_information": {"group_number": "N/A"}},
    )
    metadata: dict = {}
    add_ivr_call_data_metadata(form, metadata)
    # only member_id survives: the "N/A" group and every missing field are dropped, so the
    # navigator falls back to neutral phrasing instead of speaking a placeholder.
    assert metadata["ivr_call_data"] == {"member_id": "M1"}


def test_attaches_nothing_when_form_has_no_identifiers() -> None:
    form = PatientForm(patient_name=None, member_id=None, patient_dob=None, intake_payload={})
    metadata: dict = {}
    add_ivr_call_data_metadata(form, metadata)
    assert "ivr_call_data" not in metadata


def test_result_round_trips_through_the_schema() -> None:
    # What the dispatcher attaches must parse cleanly back into IvrCallData on the worker side.
    form = PatientForm(
        patient_name="jane roe",
        member_id="ZZZ123",
        patient_dob=date(1990, 3, 7),
        intake_payload={},
    )
    metadata: dict = {}
    add_ivr_call_data_metadata(form, metadata)
    parsed = IvrCallData.model_validate(metadata["ivr_call_data"])
    assert parsed.member_id == "ZZZ123"
    assert parsed.date_of_birth == "03/07/1990"
