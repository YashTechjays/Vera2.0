"""PromptDocument shape + content validation against a pinned schema."""

from typing import Any

import pytest
from pydantic import ValidationError

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    SessionBlock,
    validate_prompt_document,
)

SESSION: dict[str, Any] = {
    "persona": "You are VERA.",
    "goal": "Verify benefits.",
    "base_instructions": "Ask one question at a time.",
}


def schema_doc() -> FormSchemaDoc:
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "infertility_treatment",
            "system_fields": {"member_id": "sections.basics.plan_type"},
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
                        "bg": {"type": "text", "title": "Background", "role": "context"},
                    },
                }
            },
            "tasks": [{"task_key": "main", "title": "Main", "sections": ["basics"]}],
        }
    )


def prompt_doc(**overrides: Any) -> PromptDocument:
    data: dict[str, Any] = {"kind": "prompt_document", "session": SESSION, "task_overrides": {}}
    data.update(overrides)
    return PromptDocument.model_validate(data)


class TestShape:
    def test_valid_document(self) -> None:
        doc = prompt_doc(task_overrides={"main": {"prompt": "Do it politely."}})
        assert doc.task_overrides["main"].prompt == "Do it politely."

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptDocument.model_validate(
                {"kind": "prompt_document", "session": SESSION, "bogus": 1}
            )

    def test_empty_session_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptDocument.model_validate(
                {"kind": "prompt_document", "session": {**SESSION, "persona": ""}}
            )

    def test_empty_string_override_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptDocument.model_validate(
                {
                    "kind": "prompt_document",
                    "session": SESSION,
                    "task_overrides": {"main": {"intro": ""}},
                }
            )

    def test_factory_session_is_complete_and_placeholder_free(self) -> None:
        assert isinstance(FACTORY_SESSION, SessionBlock)
        for text in (
            FACTORY_SESSION.persona,
            FACTORY_SESSION.goal,
            FACTORY_SESSION.base_instructions,
        ):
            assert text and "{{" not in text


class TestContentValidation:
    def test_clean_document_has_no_errors(self) -> None:
        doc = prompt_doc(
            task_overrides={"main": {"intro": "About {{member_id}} and {{sections.basics.bg}}."}}
        )
        assert validate_prompt_document(doc, schema_doc()) == []

    def test_unknown_task_key(self) -> None:
        doc = prompt_doc(task_overrides={"ghost": {"prompt": "x"}})
        assert any("unknown task_key" in e for e in validate_prompt_document(doc, schema_doc()))

    def test_empty_override_entry(self) -> None:
        doc = prompt_doc(task_overrides={"main": {}})
        assert any("empty override" in e for e in validate_prompt_document(doc, schema_doc()))

    def test_bad_placeholder_in_session(self) -> None:
        doc = prompt_doc(session={**SESSION, "persona": "I serve {{patietn_name}}."})
        assert any("unknown placeholder" in e for e in validate_prompt_document(doc, schema_doc()))

    def test_value_token_exempt(self) -> None:
        doc = prompt_doc(task_overrides={"main": {"prompt": "Confirm {{value}} politely."}})
        assert validate_prompt_document(doc, schema_doc()) == []
