"""Form-schema DSL: compiler freshness, round-trip, and document validation."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import (
    FieldPrompt,
    FormSchemaDoc,
    Leaf,
    PromotedFields,
    Validation,
    compile_document,
    format_date,
    load_document,
    parse_date_format,
)

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"

# The eight patient_form columns every schema must promote, in artifact key order.
PROMOTED_COLUMNS: tuple[str, ...] = (
    "patient_name",
    "patient_dob",
    "chart_number",
    "appointment_date",
    "appointment_type",
    "member_id",
    "insurance_provider",
    "insurance_provider_phone_number",
)


def minimal_doc(**overrides: Any) -> dict[str, Any]:
    """Smallest valid document; tests mutate copies of it."""
    doc: dict[str, Any] = {
        "dsl_version": "2.1",
        "name": "Test",
        "insurance_type": "infertility_treatment",
        "system_fields": {"plan_type": "sections.basics.plan_type"},
        "promoted_fields": dict.fromkeys(PROMOTED_COLUMNS, "sections.basics.plan_type"),
        "rep_call_reference_number_field": "sections.basics.plan_type",
        "sections": {
            "basics": {
                "title": "Basics",
                "fields": {
                    "plan_type": {
                        "type": "text",
                        "title": "Plan Type",
                        "role": "ask",
                        "required": True,
                        "prompt": {"ask": "What type of plan is this?"},
                    },
                    "notes": {
                        "type": "text",
                        "title": "Notes",
                        "role": "ask",
                        "applicable_when": {
                            "field": "sections.basics.plan_type",
                            "op": "eq",
                            "value": "PPO",
                        },
                        "inapplicable_value": "N/A",
                        "prompt": {"ask": "Any notes?"},
                    },
                },
            }
        },
        "tasks": [{"task_key": "main", "title": "Main", "sections": ["basics"]}],
    }
    doc.update(overrides)
    return doc


class TestCompiledArtifacts:
    @pytest.mark.parametrize("insurance_type", sorted(SCHEMAS))
    def test_committed_artifact_is_fresh(self, insurance_type: str) -> None:
        """The committed JSON must equal a fresh compile — catches hand-edits/drift."""
        filename, build = SCHEMAS[insurance_type]
        compiled = compile_document(build())
        committed = (FORM_SCHEMA_DIR / filename).read_text()
        assert compiled == committed, f"{filename} is stale; run `just compile-schemas`"

    @pytest.mark.parametrize("insurance_type", sorted(SCHEMAS))
    def test_round_trip(self, insurance_type: str) -> None:
        filename, _build = SCHEMAS[insurance_type]
        text = (FORM_SCHEMA_DIR / filename).read_text()
        assert compile_document(load_document(text)) == text

    def test_ibv_collection_paths_are_role_filtered(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        collected = doc.collection_paths()
        assert "sections.insurance_information.policy_number" in collected  # confirm
        assert "sections.patient_information.patient_name" not in collected  # context
        assert all(
            leaf.role in ("ask", "confirm")
            for path, leaf in doc.leaf_items()
            if path in set(collected)
        )

    def test_ibv_call_opening_and_key_terms(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        intro_task = doc.tasks[0]
        assert intro_task.task_key == "introduction"
        assert intro_task.sections == ["patient_verification"]
        assert "{{patient_name}}" in (intro_task.intro or "")
        assert "{{member_id}}" in (intro_task.prompt or "")
        assert intro_task.outro == "Great, let me pull up my questions..."
        rule_keys = [r.rule_key for r in doc.flow_rules or []]
        assert rule_keys[0] == "insurance_not_active"
        rule = (doc.flow_rules or [])[0]
        assert rule.action == "terminate_call"
        assert rule.skip_to_task == "wrap_up"
        wrap_up = doc.tasks[-1]
        assert wrap_up.task_key == "wrap_up"
        assert wrap_up.intro is not None and wrap_up.outro is not None
        assert doc.stt_key_terms is not None
        assert "intrauterine insemination" in doc.stt_key_terms
        assert len(doc.stt_key_terms) <= 100

    def test_ibv_promotes_the_full_column_set(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        assert doc.promoted_fields == PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.insurance_information.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        )

    def test_disease_only_promotes_the_full_column_set(self) -> None:
        doc = SCHEMAS["disease_only"][1]()
        assert doc.promoted_fields == PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.policy_details.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        )

    def test_ibv_rep_call_reference_number_field(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        assert (
            doc.rep_call_reference_number_field
            == "sections.insurance_representative.call_reference_number"
        )

    def test_disease_only_rep_call_reference_number_field(self) -> None:
        doc = SCHEMAS["disease_only"][1]()
        assert (
            doc.rep_call_reference_number_field
            == "sections.representative_details.call_reference_number"
        )


class TestDocumentValidation:
    def test_minimal_doc_is_valid(self) -> None:
        FormSchemaDoc.model_validate(minimal_doc())

    def test_unresolved_condition_path_rejected(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["notes"]["applicable_when"]["field"] = (
            "sections.basics.missing"
        )
        with pytest.raises(ValidationError, match="does not resolve"):
            FormSchemaDoc.model_validate(doc)

    def test_ask_field_without_prompt_rejected(self) -> None:
        doc = minimal_doc()
        del doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]
        with pytest.raises(ValidationError, match=r"needs prompt\.ask"):
            FormSchemaDoc.model_validate(doc)

    def test_inapplicable_value_requires_gate(self) -> None:
        doc = minimal_doc()
        del doc["sections"]["basics"]["fields"]["notes"]["applicable_when"]
        with pytest.raises(ValidationError, match="inapplicable_value"):
            FormSchemaDoc.model_validate(doc)

    def test_gated_system_field_without_default_rejected(self) -> None:
        # `required_intake_fields` demands it regardless of applicability, so a gated
        # target is unfillable whenever its gate is off — both create paths would 422.
        doc = minimal_doc(
            system_fields={
                "plan_type": "sections.basics.plan_type",
                "notes": "sections.basics.notes",
            }
        )
        with pytest.raises(ValidationError, match="applicable_when gate"):
            FormSchemaDoc.model_validate(doc)

    def test_gated_system_field_with_default_accepted(self) -> None:
        # A default exempts the target from intake requiredness, so gating is safe.
        doc = minimal_doc(
            system_fields={
                "plan_type": "sections.basics.plan_type",
                "notes": "sections.basics.notes",
            }
        )
        doc["sections"]["basics"]["fields"]["notes"]["default"] = "N/A"
        FormSchemaDoc.model_validate(doc)

    def test_collect_section_must_be_tasked(self) -> None:
        doc = minimal_doc(tasks=[])
        with pytest.raises(ValidationError, match="not assigned to any task"):
            FormSchemaDoc.model_validate(doc)

    def test_duplicate_ask_group_member_rejected(self) -> None:
        doc = minimal_doc()
        member = "sections.basics.plan_type"
        doc["sections"]["basics"]["ask_groups"] = [
            {"fields": [member, "sections.basics.notes"], "ask": "Both?"},
            {"fields": [member, "sections.basics.notes"], "ask": "Again?"},
        ]
        with pytest.raises(ValidationError, match="more than one ask group"):
            FormSchemaDoc.model_validate(doc)

    def test_range_only_on_numeric_types(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["validation"] = {"range": {"min": 0}}
        with pytest.raises(ValidationError, match=r"validation\.range"):
            FormSchemaDoc.model_validate(doc)

    def test_contradiction_fields_must_be_collectable(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["shown"] = {
            "type": "text",
            "title": "Shown",
            "role": "input",
        }
        doc["contradictions"] = [
            {
                "rule_key": "bad",
                "when": {"field": "sections.basics.plan_type", "op": "eq", "value": "PPO"},
                "fields": ["sections.basics.shown"],
                "reason": "test",
            }
        ]
        with pytest.raises(ValidationError, match="re-clarified"):
            FormSchemaDoc.model_validate(doc)

    def test_unknown_task_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["intro"] = "Calling about {{patient_name}}."
        with pytest.raises(ValidationError, match="unknown placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_known_task_placeholder_accepted(self) -> None:
        doc = minimal_doc(system_fields={"plan_type": "sections.basics.plan_type"})
        doc["tasks"][0]["prompt"] = "Mention {{plan_type}} when asked."
        FormSchemaDoc.model_validate(doc)

    def test_unclosed_braces_are_not_placeholders(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["intro"] = "This {{ is not a placeholder."
        FormSchemaDoc.model_validate(doc)

    def test_malformed_placeholder_with_spaces_rejected(self) -> None:
        doc = minimal_doc(system_fields={"member_id": "sections.basics.plan_type"})
        doc["tasks"][0]["intro"] = "Your id is {{ member_id }}."
        with pytest.raises(ValidationError, match="malformed placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_malformed_placeholder_bad_chars_rejected(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["prompt"] = "Mention {{patient-name}} politely."
        with pytest.raises(ValidationError, match="malformed placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_stt_key_terms_valid_list_accepted(self) -> None:
        FormSchemaDoc.model_validate(minimal_doc(stt_key_terms=["coinsurance", "IVF"]))

    def test_stt_key_terms_duplicate_rejected(self) -> None:
        doc = minimal_doc(stt_key_terms=["IVF", "ivf"])
        with pytest.raises(ValidationError, match="duplicate term"):
            FormSchemaDoc.model_validate(doc)

    def test_stt_key_terms_empty_or_untrimmed_rejected(self) -> None:
        for bad in ["", " coinsurance", "coinsurance "]:
            with pytest.raises(ValidationError, match="empty or untrimmed"):
                FormSchemaDoc.model_validate(minimal_doc(stt_key_terms=[bad]))

    def test_stt_key_terms_placeholder_rejected(self) -> None:
        doc = minimal_doc(stt_key_terms=["{{patient_name}}"])
        with pytest.raises(ValidationError, match="placeholders are not allowed"):
            FormSchemaDoc.model_validate(doc)

    def test_stt_key_terms_cap_enforced(self) -> None:
        doc = minimal_doc(stt_key_terms=[f"term {i}" for i in range(101)])
        with pytest.raises(ValidationError, match="exceeds limit"):
            FormSchemaDoc.model_validate(doc)

    def test_empty_sections_ritual_task_is_valid(self) -> None:
        doc = minimal_doc()
        doc["tasks"].insert(0, {"task_key": "ritual", "title": "Ritual", "sections": []})
        FormSchemaDoc.model_validate(doc)

    @staticmethod
    def _context_confirm(cit: object) -> dict[str, Any]:
        """minimal_doc + a context section holding one confirm_in_task field."""
        doc = minimal_doc()
        doc["sections"]["ctx"] = {
            "title": "Ctx",
            "role": "context",
            "fields": {
                "spouse": {
                    "type": "text",
                    "title": "Spouse",
                    "role": "confirm",
                    "applicable_when": {
                        "field": "sections.basics.plan_type",
                        "op": "eq",
                        "value": "PPO",
                    },
                    "confirm_in_task": cit,
                    "prompt": {
                        "confirm": "Spouse is {{value}} — correct?",
                        "ask": "Can I get the spouse's name?",
                    },
                }
            },
        }
        return doc

    def test_confirm_in_task_object_form_required(self) -> None:
        with pytest.raises(ValidationError):
            FormSchemaDoc.model_validate(self._context_confirm("main"))
        FormSchemaDoc.model_validate(
            self._context_confirm({"task_key": "main", "confirm_immediate": True})
        )

    def test_confirm_immediate_requires_in_task_anchor(self) -> None:
        doc = self._context_confirm({"task_key": "main", "confirm_immediate": True})
        del doc["sections"]["ctx"]["fields"]["spouse"]["applicable_when"]
        with pytest.raises(ValidationError, match="needs an anchor"):
            FormSchemaDoc.model_validate(doc)

    def test_confirm_at_task_end_needs_no_anchor(self) -> None:
        doc = self._context_confirm({"task_key": "main", "confirm_immediate": False})
        del doc["sections"]["ctx"]["fields"]["spouse"]["applicable_when"]
        FormSchemaDoc.model_validate(doc)

    def test_confirm_in_task_unknown_task_rejected(self) -> None:
        doc = self._context_confirm({"task_key": "ghost", "confirm_immediate": False})
        with pytest.raises(ValidationError, match="unknown task"):
            FormSchemaDoc.model_validate(doc)

    def test_context_leaf_path_placeholder_accepted(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["bg"] = {
            "type": "text",
            "title": "Background",
            "role": "context",
        }
        doc["tasks"][0]["intro"] = "About {{sections.basics.bg}}."
        FormSchemaDoc.model_validate(doc)

    def test_non_context_leaf_path_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["intro"] = "About {{sections.basics.plan_type}}."
        with pytest.raises(ValidationError, match="unknown placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_confirm_immediate_anchor_through_nested_group_gate(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["panel"] = {
            "type": "group",
            "title": "Panel",
            "applicable_when": {
                "field": "sections.basics.plan_type",
                "op": "eq",
                "value": "PPO",
            },
            "fields": {
                "inner": {
                    "type": "text",
                    "title": "Inner",
                    "role": "ask",
                    "prompt": {"ask": "Inner?"},
                }
            },
        }
        doc["sections"]["ctx"] = {
            "title": "Ctx",
            "role": "context",
            "fields": {
                "spouse": {
                    "type": "text",
                    "title": "Spouse",
                    "role": "confirm",
                    "applicable_when": {
                        "field": "sections.basics.panel.inner",
                        "op": "eq",
                        "value": "x",
                    },
                    "confirm_in_task": {"task_key": "main", "confirm_immediate": True},
                    "prompt": {
                        "confirm": "Spouse is {{value}}?",
                        "ask": "Can I get the spouse's name?",
                    },
                }
            },
        }
        FormSchemaDoc.model_validate(doc)  # anchor found via the group-gated leaf

    def test_promoted_fields_block_is_required(self) -> None:
        doc = minimal_doc()
        del doc["promoted_fields"]
        with pytest.raises(ValidationError, match="Field required"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_every_column_is_required(self) -> None:
        doc = minimal_doc()
        del doc["promoted_fields"]["member_id"]
        with pytest.raises(ValidationError, match="Field required"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_unknown_column(self) -> None:
        doc = minimal_doc()
        doc["promoted_fields"]["not_a_column"] = "sections.basics.plan_type"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_path_not_a_leaf(self) -> None:
        doc = minimal_doc()
        doc["promoted_fields"]["patient_name"] = "sections.basics.missing"
        with pytest.raises(ValidationError, match="does not resolve to a leaf"):
            FormSchemaDoc.model_validate(doc)

    def test_promoted_fields_rejects_path_not_backed_by_system_fields(self) -> None:
        # sections.basics.notes is a real leaf but not a system_fields target.
        doc = minimal_doc()
        doc["promoted_fields"]["patient_name"] = "sections.basics.notes"
        with pytest.raises(ValidationError, match="not a system_fields target"):
            FormSchemaDoc.model_validate(doc)

    def test_rep_call_reference_number_field_is_required(self) -> None:
        doc = minimal_doc()
        del doc["rep_call_reference_number_field"]
        with pytest.raises(ValidationError):
            FormSchemaDoc.model_validate(doc)

    def test_rep_call_reference_number_field_rejects_path_not_a_leaf(self) -> None:
        doc = minimal_doc()
        doc["rep_call_reference_number_field"] = "sections.basics.missing"
        with pytest.raises(ValidationError, match="does not resolve to a leaf"):
            FormSchemaDoc.model_validate(doc)

    def test_leaf_prompt_unknown_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]["ask"] = (
            "What is the {{not_a_token}}?"
        )
        with pytest.raises(ValidationError, match="unknown placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_leaf_prompt_malformed_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]["ask"] = (
            "What is the {{ value }}?"
        )
        with pytest.raises(ValidationError, match="malformed placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_value_token_rejected_in_prompt_ask(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]["ask"] = "Is it {{value}}?"
        with pytest.raises(ValidationError, match=r"only valid in a confirm-role"):
            FormSchemaDoc.model_validate(doc)

    def test_value_token_allowed_in_confirm_prompt(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["member_id"] = {
            "type": "text",
            "title": "Policy / Member ID",
            "role": "confirm",
            "prompt": {
                "confirm": "I have the member ID as {{value}} — right?",
                "ask": "Can I get the member ID?",
            },
        }
        FormSchemaDoc.model_validate(doc)


class TestParseDateFormat:
    """`parse_date_format` — the display/entry `date_format` fallback parser used
    when a human-typed value (e.g. from the review UI) doesn't parse as ISO."""

    def test_parses_m_d_yyyy(self) -> None:
        assert parse_date_format("12/4/1999", "M/D/YYYY") == date(1999, 12, 4)

    def test_parses_with_leading_zeros(self) -> None:
        assert parse_date_format("04/12/1990", "M/D/YYYY") == date(1990, 4, 12)

    def test_parses_dd_mm_yyyy_with_dash_separator(self) -> None:
        assert parse_date_format("04-12-1990", "DD-MM-YYYY") == date(1990, 12, 4)

    def test_rejects_shape_mismatch(self) -> None:
        assert parse_date_format("1990-04-12", "M/D/YYYY") is None

    def test_rejects_wrong_separator(self) -> None:
        assert parse_date_format("12-4-1999", "M/D/YYYY") is None

    def test_rejects_out_of_range_calendar_date(self) -> None:
        assert parse_date_format("13/45/1999", "M/D/YYYY") is None

    def test_rejects_empty_string(self) -> None:
        assert parse_date_format("", "M/D/YYYY") is None

    def test_never_raises_on_a_grammar_valid_repeated_token_format(self) -> None:
        # "M/M/YYYY" passes DATE_FORMAT_RE (repeated tokens aren't shape-illegal)
        # but would build a regex with two `month` groups — must not crash.
        assert parse_date_format("12/4/1999", "M/M/YYYY") is None


class TestFormatDate:
    """`format_date` — the inverse of `parse_date_format`: renders a parsed `date`
    back into a leaf's declared display/entry `date_format`, so a date leaf's
    stored answer always matches that format regardless of how it was submitted."""

    def test_renders_m_d_yyyy_without_padding(self) -> None:
        assert format_date(date(1999, 12, 4), "M/D/YYYY") == "12/4/1999"

    def test_renders_single_digit_month_and_day_without_padding(self) -> None:
        assert format_date(date(2026, 7, 1), "M/D/YYYY") == "7/1/2026"

    def test_pads_to_mm_dd_yyyy(self) -> None:
        assert format_date(date(2026, 7, 1), "MM/DD/YYYY") == "07/01/2026"

    def test_renders_dd_mm_yyyy_with_dash_separator(self) -> None:
        assert format_date(date(1990, 12, 4), "DD-MM-YYYY") == "04-12-1990"

    def test_round_trips_through_parse_date_format(self) -> None:
        for text, fmt in [("12/4/1999", "M/D/YYYY"), ("07/01/2026", "MM/DD/YYYY")]:
            parsed = parse_date_format(text, fmt)
            assert parsed is not None
            assert format_date(parsed, fmt) == text


class TestDateFormatRejectsTwoDigitYear:
    """A 2-digit year is unsafe on a DOB field (e.g. "55" is ambiguous between
    1955 and 2055) — rejected at schema-authoring time, not just at parse time."""

    def test_yyyy_is_accepted(self) -> None:
        Validation(date_format="M/D/YYYY")

    def test_yy_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="date_format"):
            Validation(date_format="M/D/YY")


class TestDateFormatRequiresOneOfEachToken:
    """A `date_format` missing a token ("MM/YYYY" — `format_date` would silently
    drop the day from every stored value) or repeating one ("M/M/YYYY" — renders
    the month twice, and `parse_date_format` can never match it) is lossy, so it's
    rejected at schema-authoring time, before any value can be corrupted."""

    def test_each_complete_format_is_accepted(self) -> None:
        for fmt in ["M/D/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "YYYY.MM.DD"]:
            Validation(date_format=fmt)

    def test_missing_day_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="date_format"):
            Validation(date_format="MM/YYYY")

    def test_missing_year_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="date_format"):
            Validation(date_format="M/D")

    def test_repeated_month_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="date_format"):
            Validation(date_format="M/M/YYYY")

    def test_repeated_day_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="date_format"):
            Validation(date_format="D/DD/YYYY")


def test_confirm_leaf_accepts_prompt_ask() -> None:
    leaf = Leaf(
        type="text",
        title="Policy / Member ID",
        role="confirm",
        prompt=FieldPrompt(
            confirm="I have the member ID as {{value}} — can you confirm that is correct?",
            ask="Can I get the member ID for this policy?",
        ),
    )
    assert leaf.prompt is not None
    assert leaf.prompt.ask == "Can I get the member ID for this policy?"


def test_confirm_leaf_without_prompt_ask_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"confirm field needs prompt\.ask"):
        Leaf(
            type="text",
            title="Policy / Member ID",
            role="confirm",
            prompt=FieldPrompt(confirm="I have the member ID as {{value}} — right?"),
        )


def test_prompt_ask_still_rejected_on_context_role() -> None:
    with pytest.raises(ValidationError, match=r"prompt\.ask on role context"):
        Leaf(
            type="text",
            title="Spouse Gender",
            role="context",
            prompt=FieldPrompt(ask="What is the spouse's gender?"),
        )


class TestPromotedColumnParity:
    """PromotedFields (DSL contract), PromotedIdentifiers (intake value carrier) and
    PatientForm (the table) must agree on the promoted column set — a future column
    add that misses one of the three fails here, not in production."""

    # The documented contract: PatientForm's promoted searchable-identifier +
    # worklist-display columns. PatientForm has many non-promoted columns, so
    # this literal — not introspection — defines "promoted".
    EXPECTED = frozenset(PROMOTED_COLUMNS)

    def test_dsl_model_matches_the_contract(self) -> None:
        assert set(PromotedFields.model_fields) == self.EXPECTED

    def test_intake_dataclass_matches_the_contract(self) -> None:
        from dataclasses import fields as dataclass_fields

        from vera_core.forms.intake import PromotedIdentifiers

        assert {f.name for f in dataclass_fields(PromotedIdentifiers)} == self.EXPECTED

    def test_patient_form_table_has_every_promoted_column(self) -> None:
        from vera_core.models.patient_form import PatientForm

        assert {c.name for c in PatientForm.__table__.columns} >= self.EXPECTED
