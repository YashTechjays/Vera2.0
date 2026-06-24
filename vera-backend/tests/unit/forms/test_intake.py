"""Pure-logic tests for the IBV intake helpers (no DB)."""

from datetime import date

import pytest

from vera_core.forms.intake import (
    InvalidIntakeValue,
    iter_leaf_answers,
    missing_required,
    promote_columns,
    required_intake_fields,
)

# A minimal stand-in for schema_version.schema_json — only the bits the helpers read.
SCHEMA = {
    "sections": [
        {
            "section_key": "patient_information",
            "required": ["patient_name", "patient_dob", "patient_gender"],
            "properties": {},
        },
        {
            "section_key": "appointment_information",
            "required": ["appointment_date"],
            "properties": {},
        },
    ]
}


class TestRequiredIntakeFields:
    def test_reads_patient_information_required(self) -> None:
        assert required_intake_fields(SCHEMA) == [
            "patient_name",
            "patient_dob",
            "patient_gender",
        ]

    def test_empty_when_no_patient_information_section(self) -> None:
        assert required_intake_fields({"sections": []}) == []


class TestMissingRequired:
    def test_empty_payload_lists_all_required_as_paths(self) -> None:
        assert missing_required({}, SCHEMA) == [
            "patient_information.patient_name",
            "patient_information.patient_dob",
            "patient_information.patient_gender",
        ]

    def test_filled_required_yields_nothing(self) -> None:
        payload = {
            "patient_information": {
                "patient_name": "Jane Doe",
                "patient_dob": "1990-04-12",
                "patient_gender": "Female",
            }
        }
        assert missing_required(payload, SCHEMA) == []

    def test_blank_string_counts_as_missing(self) -> None:
        payload = {
            "patient_information": {
                "patient_name": "  ",
                "patient_dob": "1990-04-12",
                "patient_gender": "Female",
            }
        }
        assert missing_required(payload, SCHEMA) == ["patient_information.patient_name"]


class TestIterLeafAnswers:
    def test_flattens_nested_payload_to_dotted_paths(self) -> None:
        payload = {
            "patient_information": {"patient_name": "Jane Doe", "patient_dob": "1990-04-12"},
            "coverages": {"general_coverage": {"office_visit": {"covered": "Yes"}}},
        }
        assert sorted(iter_leaf_answers(payload)) == [
            ("coverages.general_coverage.office_visit.covered", "Yes"),
            ("patient_information.patient_dob", "1990-04-12"),
            ("patient_information.patient_name", "Jane Doe"),
        ]

    def test_skips_empty_leaves_and_empty_objects(self) -> None:
        payload = {
            "patient_information": {"patient_name": "Jane Doe", "chart_number": ""},
            "appointment_information": {},
            "benefit_coverage": {"coverage_type": None},
        }
        assert list(iter_leaf_answers(payload)) == [
            ("patient_information.patient_name", "Jane Doe")
        ]


class TestPromoteColumns:
    def test_maps_and_normalizes(self) -> None:
        payload = {
            "patient_information": {
                "patient_name": "  Jane Doe  ",
                "patient_dob": "1990-04-12",
                "chart_number": "  C-100 ",
            },
            "appointment_information": {
                "appointment_date": "2026-07-01",
                "appointment_type": "  New Patient ",
            },
            "insurance_information": {"policy_number": "  POL-42 "},
            "insurance_reference_information": {
                "insurance": "  Blue Cross ",
                "phone_number": " +1 555 0100 ",
            },
        }
        promoted = promote_columns(payload)
        assert promoted.patient_name == "jane doe"
        assert promoted.chart_number == "C-100"
        assert promoted.patient_dob == date(1990, 4, 12)
        assert promoted.appointment_date == date(2026, 7, 1)
        assert promoted.member_id is None
        # Display fields: trimmed, kept verbatim (no case folding).
        assert promoted.appointment_type == "New Patient"
        assert promoted.member_policy_id == "POL-42"
        assert promoted.insurance_provider == "Blue Cross"
        assert promoted.insurance_provider_phone_number == "+1 555 0100"

    def test_chart_number_na_becomes_none(self) -> None:
        payload = {"patient_information": {"chart_number": "N/A"}}
        assert promote_columns(payload).chart_number is None

    def test_absent_fields_are_none(self) -> None:
        promoted = promote_columns({})
        assert promoted.patient_name is None
        assert promoted.patient_dob is None
        assert promoted.appointment_date is None
        assert promoted.chart_number is None
        assert promoted.member_id is None
        assert promoted.appointment_type is None
        assert promoted.member_policy_id is None
        assert promoted.insurance_provider is None
        assert promoted.insurance_provider_phone_number is None

    def test_bad_date_raises_with_field_path(self) -> None:
        payload = {"patient_information": {"patient_dob": "12/04/1990"}}
        with pytest.raises(InvalidIntakeValue) as exc:
            promote_columns(payload)
        assert exc.value.field_path == "patient_information.patient_dob"
