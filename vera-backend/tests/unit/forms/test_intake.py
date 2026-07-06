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

# A minimal, fully valid v2 document spanning several sections — used to prove
# required-at-intake resolution is driven entirely by the schema's own
# `system_fields` block (dynamic per-schema, no hardcoded section/role list): a
# `system_fields` target is required unless its leaf declares a `default`;
# everything else — the DSL's `required`/`role`, which govern voice collection and
# gap analysis ("form filling") — is irrelevant to what a schema needs at creation.
V2_SCHEMA = {
    "dsl_version": "2.1",
    "name": "Test v2 Intake",
    "insurance_type": "test_type",
    "system_fields": {
        "patient_name": "sections.patient_information.patient_name",
        "hospital_npi": "sections.hospital_information.npi",
        "verified_by": "sections.verification_information.verified_by",
        # Two handles aliasing the same leaf — must not double-report it.
        "form_queued_by": "sections.verification_information.verified_by",
        # Carries a default — filled even if the payload omits it.
        "callback_number": "sections.verification_information.callback_number",
        # role=confirm — still required at intake despite not being ask/context.
        "policy_id": "sections.insurance_information.policy_number",
    },
    "sections": {
        "patient_information": {
            "title": "Patient Information",
            "role": "context",
            "fields": {
                "patient_name": {
                    "type": "text",
                    "title": "Patient Name",
                    "role": "context",
                    "required": True,
                },
                # `required: true` but NOT a system_fields target — must be
                # ignored at creation ("form filling" concern, not creation).
                "patient_dob": {
                    "type": "date",
                    "title": "Patient DOB",
                    "role": "context",
                    "required": True,
                },
            },
        },
        "hospital_information": {
            "title": "Hospital Information",
            "role": "context",
            "fields": {
                "npi": {"type": "text", "title": "Facility NPI", "role": "context"},
            },
        },
        "verification_information": {
            "title": "Verification Information",
            "role": "context",
            "fields": {
                "verified_by": {"type": "text", "title": "Verified By", "role": "context"},
                "callback_number": {
                    "type": "phone",
                    "title": "Callback Number",
                    "role": "context",
                    "default": "N/A",
                },
            },
        },
        "insurance_information": {
            "title": "Insurance Information",
            "fields": {
                "policy_number": {
                    "type": "text",
                    "title": "Policy Number",
                    "role": "confirm",
                    "required": True,
                    "prompt": {"confirm": "I have {{value}} — can you confirm?"},
                },
            },
        },
    },
    "tasks": [{"task_key": "main", "title": "Main", "sections": ["insurance_information"]}],
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


class TestRequiredIntakeFieldsV2:
    def test_matches_system_fields_targets_without_a_default(self) -> None:
        assert required_intake_fields(V2_SCHEMA) == [
            "sections.patient_information.patient_name",
            "sections.hospital_information.npi",
            "sections.verification_information.verified_by",
            "sections.insurance_information.policy_number",
        ]

    def test_excludes_fields_with_a_default(self) -> None:
        assert "sections.verification_information.callback_number" not in required_intake_fields(
            V2_SCHEMA
        )

    def test_excludes_required_fields_that_are_not_system_fields(self) -> None:
        # `patient_dob` is `required: true` in the schema but not a system_fields
        # target — that's a "form filling" concern (voice/gap-analysis), not a
        # creation-time one.
        assert "sections.patient_information.patient_dob" not in required_intake_fields(V2_SCHEMA)

    def test_two_handles_aliasing_the_same_leaf_report_once(self) -> None:
        # `verified_by` and `form_queued_by` both point at the same leaf.
        required = required_intake_fields(V2_SCHEMA)
        assert required.count("sections.verification_information.verified_by") == 1


class TestMissingRequiredV2:
    def test_empty_payload_lists_every_system_field_without_a_default(self) -> None:
        assert missing_required({}, V2_SCHEMA) == [
            "sections.patient_information.patient_name",
            "sections.hospital_information.npi",
            "sections.verification_information.verified_by",
            "sections.insurance_information.policy_number",
        ]

    def test_catches_a_missing_system_field_outside_patient_information(self) -> None:
        # Regression: `missing_required` used to only inspect `patient_information`,
        # so a system field declared in any other section (e.g. this
        # hospital_information system field) silently passed even when absent.
        payload = {
            "patient_information": {"patient_name": "Jane Doe"},
            "verification_information": {"verified_by": "Dr. Reyes"},
            "insurance_information": {"policy_number": "POL-1"},
        }
        assert missing_required(payload, V2_SCHEMA) == ["sections.hospital_information.npi"]

    def test_fully_filled_yields_nothing(self) -> None:
        payload = {
            "patient_information": {"patient_name": "Jane Doe"},
            "hospital_information": {"npi": "1234567890"},
            "verification_information": {"verified_by": "Dr. Reyes"},
            "insurance_information": {"policy_number": "POL-1"},
        }
        assert missing_required(payload, V2_SCHEMA) == []

    def test_field_with_a_default_never_blocks_creation(self) -> None:
        payload = {
            "patient_information": {"patient_name": "Jane Doe"},
            "hospital_information": {"npi": "1234567890"},
            "verification_information": {"verified_by": "Dr. Reyes"},
            "insurance_information": {"policy_number": "POL-1"},
        }
        # callback_number is absent but has a default — never reported missing.
        assert missing_required(payload, V2_SCHEMA) == []

    def test_required_field_that_is_not_a_system_field_is_ignored(self) -> None:
        payload = {
            "patient_information": {"patient_name": "Jane Doe"},  # patient_dob absent
            "hospital_information": {"npi": "1234567890"},
            "verification_information": {"verified_by": "Dr. Reyes"},
            "insurance_information": {"policy_number": "POL-1"},
        }
        assert missing_required(payload, V2_SCHEMA) == []


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
