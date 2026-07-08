"""Form-schema DSL: compiler freshness, round-trip, and document validation."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import FormSchemaDoc, compile_document, load_document

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"


def minimal_doc(**overrides: Any) -> dict[str, Any]:
    """Smallest valid document; tests mutate copies of it."""
    doc: dict[str, Any] = {
        "dsl_version": "2.1",
        "name": "Test",
        "insurance_type": "infertility_treatment",
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
