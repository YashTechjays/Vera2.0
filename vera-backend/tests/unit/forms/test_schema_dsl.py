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

    def test_ibv_call_opening_and_key_terms(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        intro_task = doc.tasks[0]
        assert intro_task.task_key == "introduction"
        assert intro_task.sections == ["patient_verification"]
        assert "{{patient_name}}" in (intro_task.intro or "")
        assert "{{member_id}}" in (intro_task.prompt or "")
        assert intro_task.outro == "Great, let me pull up my questions..."
        rule_keys = [r.rule_key for r in doc.flow_rules or []]
        assert rule_keys[0] == "patient_not_on_plan"
        rule = (doc.flow_rules or [])[0]
        assert rule.action == "terminate_call"
        assert rule.skip_to_task == "wrap_up"
        wrap_up = doc.tasks[-1]
        assert wrap_up.task_key == "wrap_up"
        assert wrap_up.intro is not None and wrap_up.outro is not None
        assert doc.stt_key_terms is not None
        assert "intrauterine insemination" in doc.stt_key_terms
        assert len(doc.stt_key_terms) <= 100


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
                    "prompt": {"confirm": "Spouse is {{value}} — correct?"},
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
