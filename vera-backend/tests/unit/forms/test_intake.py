"""Pure-logic tests for the IBV intake helpers (no DB)."""

from datetime import date
from typing import Any

import pytest

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import (
    InvalidIntakeValue,
    date_leaf_paths,
    iter_leaf_answers,
    missing_required,
    normalize_date_answers,
    normalize_date_value,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    required_intake_fields,
    resolve_path,
    validate_enum_answers,
)

from .test_call_plan import IBV

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
    # promoted_fields is unrelated to this fixture's actual purpose (required_intake_fields
    # / missing_required only read system_fields) but is now a required block on every v2
    # document — all eight columns share one already-declared system_fields target.
    "promoted_fields": {
        "patient_name": "sections.patient_information.patient_name",
        "patient_dob": "sections.patient_information.patient_name",
        "chart_number": "sections.patient_information.patient_name",
        "appointment_date": "sections.patient_information.patient_name",
        "appointment_type": "sections.patient_information.patient_name",
        "member_id": "sections.patient_information.patient_name",
        "insurance_provider": "sections.patient_information.patient_name",
        "insurance_provider_phone_number": "sections.patient_information.patient_name",
    },
    "rep_call_reference_number_field": "sections.patient_information.patient_name",
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
                    "prompt": {
                        "confirm": "I have {{value}} — can you confirm?",
                        "ask": "Can I get the policy number?",
                    },
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


_CANONICAL_PROMOTED: dict[str, str] = {
    "patient_name": "sections.patient_information.patient_name",
    "patient_dob": "sections.patient_information.patient_dob",
    "chart_number": "sections.patient_information.chart_number",
    "appointment_date": "sections.appointment_information.appointment_date",
    "appointment_type": "sections.appointment_information.appointment_type",
    "member_id": "sections.insurance_information.policy_number",
    "insurance_provider": "sections.insurance_reference_information.insurance_provider_name",
    "insurance_provider_phone_number": (
        "sections.insurance_reference_information.insurance_phone_number"
    ),
}


def _doc_with_promoted_fields(
    overrides: dict[str, str] | None = None,
    leaf_types: dict[str, str] | None = None,
    extra_leaves: dict[str, dict[str, Any]] | None = None,
) -> FormSchemaDoc:
    """A minimal v2 document promoting all eight columns (PromotedFields is total).
    `overrides` repoints individual columns; `leaf_types` repoints an individual
    promoted column's leaf `type` (default "text") — used to exercise type-specific
    promotion logic (e.g. phone). `extra_leaves` adds NON-promoted leaves at
    additional root-anchored paths (path -> leaf dict, e.g.
    "sections.patient_information.spouse_partner_dob" -> {"type": "date", ...}) —
    used to prove a behavior is keyed on `leaf.type`, not promoted_fields
    membership. system_fields (required for dsl.py validation) exactly mirror the
    merged promoted map, and every referenced path gets a context leaf."""
    promoted_fields = {**_CANONICAL_PROMOTED, **(overrides or {})}
    leaf_types = leaf_types or {}
    sections: dict[str, Any] = {}
    for column, path in promoted_fields.items():
        _, section_key, field_key = path.split(".")
        sections.setdefault(
            section_key,
            {"title": section_key, "role": "context", "fields": {}},
        )["fields"][field_key] = {
            "type": leaf_types.get(column, "text"),
            "title": field_key,
            "role": "context",
        }
    for path, leaf in (extra_leaves or {}).items():
        _, section_key, field_key = path.split(".")
        sections.setdefault(
            section_key,
            {"title": section_key, "role": "context", "fields": {}},
        )["fields"][field_key] = leaf
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "test_type",
            "system_fields": dict(promoted_fields),
            "promoted_fields": promoted_fields,
            "rep_call_reference_number_field": promoted_fields["patient_name"],
            "sections": sections,
            # All fixture sections are role="context" (no voice collection needed for
            # these tests), so none may be assigned to a task (dsl.py: "only collect
            # sections belong to tasks") — an empty task list is the valid v2 shape
            # for a document with zero collect sections.
            "tasks": [],
        }
    )


_FULL_DOC = _doc_with_promoted_fields()


class TestPromoteColumns:
    def test_maps_and_normalizes_from_a_nested_payload(self) -> None:
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
                "insurance_provider_name": "  Blue Cross ",
                "insurance_phone_number": " +1 555 0100 ",
            },
        }
        promoted = promote_columns(lambda p: resolve_path(payload, p), _FULL_DOC)
        assert promoted.patient_name == "jane doe"
        assert promoted.chart_number == "C-100"
        assert promoted.patient_dob == date(1990, 4, 12)
        assert promoted.appointment_date == date(2026, 7, 1)
        assert promoted.appointment_type == "New Patient"
        assert promoted.member_id == "POL-42"
        assert promoted.insurance_provider == "Blue Cross"
        assert promoted.insurance_provider_phone_number == "+1 555 0100"

    def test_maps_and_normalizes_from_a_flat_map(self) -> None:
        # The dispute-resolve shape: current_values keyed by root-anchored field_path.
        current_values = {
            "sections.patient_information.patient_name": "  Jane Doe  ",
            "sections.patient_information.patient_dob": "1990-04-12",
            "sections.insurance_reference_information.insurance_provider_name": "Blue Cross",
        }
        doc = _doc_with_promoted_fields()
        promoted = promote_columns(current_values.get, doc)
        assert promoted.patient_name == "jane doe"
        assert promoted.patient_dob == date(1990, 4, 12)
        assert promoted.insurance_provider == "Blue Cross"

    def test_chart_number_na_becomes_none(self) -> None:
        doc = _doc_with_promoted_fields()
        payload = {"patient_information": {"chart_number": "N/A"}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.chart_number is None

    def test_absent_payload_values_promote_to_none(self) -> None:
        # Every column is mapped, but a payload can still lack the values.
        promoted = promote_columns(lambda p: None, _FULL_DOC)
        assert promoted.patient_name is None
        assert promoted.patient_dob is None
        assert promoted.appointment_date is None
        assert promoted.chart_number is None
        assert promoted.appointment_type is None
        assert promoted.member_id is None
        assert promoted.insurance_provider is None
        assert promoted.insurance_provider_phone_number is None

    def test_bad_date_raises_with_the_schema_path(self) -> None:
        doc = _doc_with_promoted_fields()
        payload = {"patient_information": {"patient_dob": "12/04/1990"}}
        with pytest.raises(InvalidIntakeValue) as exc:
            promote_columns(lambda p: resolve_path(payload, p), doc)
        assert exc.value.field_path == "sections.patient_information.patient_dob"


def _doc_with_date_format(date_format: str) -> FormSchemaDoc:
    """A doc whose patient_dob leaf declares `validation.date_format`, mirroring
    ibv_standard.py's real `DATE_VALIDATION = Validation(date_format="M/D/YYYY")`
    — the review UI prompts for and submits values in exactly this format. The
    other seven (now-mandatory) columns share a filler leaf the tests never set."""
    dob_path = "sections.patient_information.patient_dob"
    filler_path = "sections.patient_information.filler"
    promoted_fields = {
        column: (dob_path if column == "patient_dob" else filler_path)
        for column in _CANONICAL_PROMOTED
    }
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "test_type",
            "system_fields": {"patient_dob": dob_path, "filler": filler_path},
            "promoted_fields": promoted_fields,
            "rep_call_reference_number_field": filler_path,
            "sections": {
                "patient_information": {
                    "title": "Patient Information",
                    "role": "context",
                    "fields": {
                        "patient_dob": {
                            "type": "date",
                            "title": "Patient DOB",
                            "role": "context",
                            "validation": {"date_format": date_format},
                        },
                        "filler": {"type": "text", "title": "Filler", "role": "context"},
                    },
                },
            },
            "tasks": [],
        }
    )


class TestPromoteColumnsDateFormatFallback:
    """A human editing a date field through the review UI types it in the leaf's
    declared `validation.date_format`, not ISO (intake.py's `_parse_date` only ever
    accepted ISO — this is the fallback that makes dispute-resolve date edits work)."""

    def test_falls_back_to_the_leaf_declared_date_format(self) -> None:
        doc = _doc_with_date_format("M/D/YYYY")
        payload = {"patient_information": {"patient_dob": "12/4/1999"}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.patient_dob == date(1999, 12, 4)

    def test_iso_still_works_when_a_date_format_is_declared(self) -> None:
        doc = _doc_with_date_format("M/D/YYYY")
        payload = {"patient_information": {"patient_dob": "1999-12-04"}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.patient_dob == date(1999, 12, 4)

    def test_raises_when_neither_iso_nor_the_declared_format_matches(self) -> None:
        doc = _doc_with_date_format("M/D/YYYY")
        payload = {"patient_information": {"patient_dob": "not-a-date"}}
        with pytest.raises(InvalidIntakeValue) as exc:
            promote_columns(lambda p: resolve_path(payload, p), doc)
        assert exc.value.field_path == "sections.patient_information.patient_dob"


class TestNormalizePhonePrefix:
    def test_adds_plus_when_missing(self) -> None:
        assert normalize_phone_prefix("15550001234") == "+15550001234"

    def test_leaves_existing_plus_untouched(self) -> None:
        assert normalize_phone_prefix("+15550001234") == "+15550001234"

    def test_trims_surrounding_whitespace_before_checking(self) -> None:
        assert normalize_phone_prefix("  15550001234  ") == "+15550001234"

    def test_does_not_touch_internal_separators(self) -> None:
        # Adding '+' is the only reformatting — a value with internal spaces/dashes
        # still isn't E.164-shaped, and that's left to the validation step.
        assert normalize_phone_prefix("555-000-1234") == "+555-000-1234"

    def test_blank_string_passes_through_untouched(self) -> None:
        assert normalize_phone_prefix("") == ""
        assert normalize_phone_prefix("   ") == "   "

    def test_non_string_passes_through_untouched(self) -> None:
        assert normalize_phone_prefix(None) is None


class TestPhonePromotedPaths:
    def test_finds_the_phone_typed_promoted_column(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        assert phone_promoted_paths(doc) == {
            "sections.insurance_reference_information.insurance_phone_number"
        }

    def test_empty_when_no_promoted_column_is_phone_typed(self) -> None:
        assert phone_promoted_paths(_FULL_DOC) == set()


class TestNormalizePhoneAnswers:
    def test_prefixes_only_the_phone_promoted_path(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        answers = [
            ("sections.insurance_reference_information.insurance_phone_number", "15550001234"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]
        assert normalize_phone_answers(answers, doc) == [
            ("sections.insurance_reference_information.insurance_phone_number", "+15550001234"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]

    def test_no_op_when_nothing_is_phone_typed(self) -> None:
        answers = [("sections.patient_information.patient_name", "Jane Doe")]
        assert normalize_phone_answers(answers, _FULL_DOC) == answers


class TestPromoteColumnsPhone:
    """`insurance_provider_phone_number` is handled by the leaf's declared `type ==
    "phone"`, not by column name — dynamic per schema, matching every real IBV catalog
    leaf (`ibv_standard.py`'s `insurance_phone_number` is `type="phone"`). `_FULL_DOC`
    types every promoted leaf "text", so `TestPromoteColumns` above continues to
    exercise the unchanged generic path; these tests use a doc that actually types the
    column "phone"."""

    def test_missing_plus_gets_prefixed_and_accepted(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {"insurance_reference_information": {"insurance_phone_number": "15550001234"}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.insurance_provider_phone_number == "+15550001234"

    def test_already_prefixed_valid_number_is_untouched(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {"insurance_reference_information": {"insurance_phone_number": "+15550001234"}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)
        assert promoted.insurance_provider_phone_number == "+15550001234"

    def test_missing_plus_and_still_invalid_raises(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {"insurance_reference_information": {"insurance_phone_number": "555 000 1234"}}
        with pytest.raises(InvalidIntakeValue) as exc:
            promote_columns(lambda p: resolve_path(payload, p), doc)
        assert (
            exc.value.field_path
            == "sections.insurance_reference_information.insurance_phone_number"
        )

    def test_already_prefixed_but_invalid_raises(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        payload = {"insurance_reference_information": {"insurance_phone_number": "+1 555 0100"}}
        with pytest.raises(InvalidIntakeValue):
            promote_columns(lambda p: resolve_path(payload, p), doc)

    def test_absent_value_stays_none_with_no_validation_error(self) -> None:
        doc = _doc_with_promoted_fields(leaf_types={"insurance_provider_phone_number": "phone"})
        promoted = promote_columns(lambda p: None, doc)
        assert promoted.insurance_provider_phone_number is None

    def test_non_phone_typed_column_keeps_the_old_whitespace_only_behavior(self) -> None:
        # Regression guard: proves the branch is keyed on leaf.type, not the column
        # name — _FULL_DOC never types this column "phone".
        payload = {"insurance_reference_information": {"insurance_phone_number": " +1 555 0100 "}}
        promoted = promote_columns(lambda p: resolve_path(payload, p), _FULL_DOC)
        assert promoted.insurance_provider_phone_number == "+1 555 0100"


_SPOUSE_DOB_PATH = "sections.patient_information.spouse_partner_dob"


def _spouse_dob_leaf(date_format: str | None) -> dict[str, Any]:
    leaf: dict[str, Any] = {"type": "date", "title": "Spouse DOB", "role": "context"}
    if date_format is not None:
        leaf["validation"] = {"date_format": date_format}
    return leaf


class TestDateLeafPaths:
    def test_finds_every_date_typed_leaf_with_its_declared_format(self) -> None:
        doc = _doc_with_promoted_fields(
            leaf_types={"patient_dob": "date"},
            extra_leaves={_SPOUSE_DOB_PATH: _spouse_dob_leaf("M/D/YYYY")},
        )
        assert date_leaf_paths(doc) == {
            "sections.patient_information.patient_dob": None,
            _SPOUSE_DOB_PATH: "M/D/YYYY",
        }

    def test_empty_when_nothing_is_date_typed(self) -> None:
        assert date_leaf_paths(_FULL_DOC) == {}


class TestNormalizeDateValue:
    def test_reformats_iso_input_to_the_declared_format(self) -> None:
        assert normalize_date_value("1999-12-04", "path", "M/D/YYYY") == "12/4/1999"

    def test_reformats_declared_format_input_to_itself(self) -> None:
        assert normalize_date_value("12/4/1999", "path", "M/D/YYYY") == "12/4/1999"

    def test_pads_to_the_declared_format_width(self) -> None:
        assert normalize_date_value("1999-12-04", "path", "MM/DD/YYYY") == "12/04/1999"

    def test_falls_back_to_iso_when_the_leaf_declares_no_format(self) -> None:
        assert normalize_date_value("1999-12-04", "path", None) == "1999-12-04"

    def test_blank_string_passes_through_untouched(self) -> None:
        assert normalize_date_value("", "path", "M/D/YYYY") == ""

    def test_none_passes_through_untouched(self) -> None:
        assert normalize_date_value(None, "path", "M/D/YYYY") is None

    def test_raises_on_an_unparseable_value(self) -> None:
        with pytest.raises(InvalidIntakeValue) as exc:
            normalize_date_value("not-a-date", "sections.a.b", "M/D/YYYY")
        assert exc.value.field_path == "sections.a.b"


class TestNormalizeDateAnswers:
    def test_reformats_only_date_typed_paths(self) -> None:
        doc = _doc_with_promoted_fields(
            extra_leaves={_SPOUSE_DOB_PATH: _spouse_dob_leaf("M/D/YYYY")}
        )
        answers = [
            (_SPOUSE_DOB_PATH, "1999-12-04"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]
        assert normalize_date_answers(answers, doc) == [
            (_SPOUSE_DOB_PATH, "12/4/1999"),
            ("sections.patient_information.patient_name", "Jane Doe"),
        ]

    def test_no_op_when_nothing_is_date_typed(self) -> None:
        answers = [("sections.patient_information.patient_name", "Jane Doe")]
        assert normalize_date_answers(answers, _FULL_DOC) == answers

    def test_raises_with_the_offending_path(self) -> None:
        doc = _doc_with_promoted_fields(
            extra_leaves={_SPOUSE_DOB_PATH: _spouse_dob_leaf("M/D/YYYY")}
        )
        answers = [(_SPOUSE_DOB_PATH, "not-a-date")]
        with pytest.raises(InvalidIntakeValue) as exc:
            normalize_date_answers(answers, doc)
        assert exc.value.field_path == _SPOUSE_DOB_PATH


_COVERAGE_TYPE_PATH = "sections.benefit_coverage.coverage_type"
_PCP_REFERRAL_PATH = "sections.benefit_coverage.pcp_referral_required"


class TestValidateEnumAnswers:
    def test_raises_with_the_offending_path_and_no_value(self) -> None:
        with pytest.raises(InvalidIntakeValue) as exc:
            validate_enum_answers([(_COVERAGE_TYPE_PATH, "PT/Spouse")], IBV)
        assert exc.value.field_path == _COVERAGE_TYPE_PATH
        assert "PT/Spouse" not in str(exc.value)

    def test_declared_value_is_accepted(self) -> None:
        validate_enum_answers([(_COVERAGE_TYPE_PATH, "Family")], IBV)

    def test_the_leafs_own_default_is_accepted(self) -> None:
        """pcp_referral_required declares default="N/A", so intake may send it."""
        validate_enum_answers([(_PCP_REFERRAL_PATH, "N/A")], IBV)

    def test_blank_value_passes_through(self) -> None:
        validate_enum_answers([(_COVERAGE_TYPE_PATH, "")], IBV)

    def test_non_enum_path_is_ignored(self) -> None:
        validate_enum_answers([("sections.insurance_information.group_name", "Anything")], IBV)
