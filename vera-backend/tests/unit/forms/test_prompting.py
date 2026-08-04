"""render_task_prompts: schema document (+ prompt document) → per-task prompt text."""

import logging
import os
from pathlib import Path

import pytest

from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    RenderedPrompts,
    RenderedTaskPrompt,
    SessionBlock,
    TaskTextOverride,
    render_task_prompts,
)

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

IBV: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
RENDERED: RenderedPrompts = render_task_prompts(IBV)


def task(key: str) -> RenderedTaskPrompt:
    return next(t for t in RENDERED.tasks if t.task_key == key)


class TestSession:
    def test_factory_fallback_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            out = render_task_prompts(IBV, None)
        assert out.persona == FACTORY_SESSION.persona
        assert any("factory session" in r.message for r in caplog.records)

    def test_session_text_is_literal(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=SessionBlock(persona="P.", goal="G.", base_instructions="B."),
        )
        out = render_task_prompts(IBV, doc)
        assert (out.persona, out.goal, out.base_instructions) == ("P.", "G.", "B.")
        # session text is never folded into task prompts
        assert all("P." not in t.prompt for t in out.tasks)


class TestTaskText:
    def test_task_order_and_meta(self) -> None:
        assert [t.task_key for t in RENDERED.tasks] == [t.task_key for t in IBV.tasks]
        assert RENDERED.name == "Infertility"
        assert RENDERED.dsl_version == "2.1"

    def test_intro_outro_pass_through(self) -> None:
        intro_task = task("introduction")
        assert intro_task.intro is not None and "{{patient_name}}" in intro_task.intro
        assert intro_task.outro == "Great, let me pull up my questions..."

    def test_override_merge_field_level(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"introduction": TaskTextOverride(intro="Hi. {{member_id}}.")},
        )
        out = render_task_prompts(IBV, doc)
        intro_task = next(t for t in out.tasks if t.task_key == "introduction")
        assert intro_task.intro == "Hi. {{member_id}}."
        # outro not overridden → schema default survives
        assert intro_task.outro == "Great, let me pull up my questions..."

    def test_blank_override_suppresses_schema_speech(self) -> None:
        # "" is an explicit "say nothing", never a fall-through to the default.
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"introduction": TaskTextOverride(intro="", outro="")},
        )
        intro_task = next(
            t for t in render_task_prompts(IBV, doc).tasks if t.task_key == "introduction"
        )
        assert (intro_task.intro, intro_task.outro) == ("", "")

    def test_unknown_override_key_ignored(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"ghost": TaskTextOverride(prompt="x")},
        )
        assert render_task_prompts(IBV, doc).tasks  # no raise

    def test_questions_render_with_vocab_and_gates(self) -> None:
        basics = task("insurance_basics").prompt
        assert "Is the doctor inside the insurance network?" in basics
        assert "Answers: Yes | No" in basics
        assert "Ask only if" in basics
        assert '"Doctor Inside Network" is "No"' in basics

    def test_immediate_confirm_attaches_to_anchor(self) -> None:
        basics = task("insurance_basics").prompt
        assert "Immediately after this answer" in basics
        assert "spouse listed" in basics  # spouse name confirm text
        assert (
            "Before finishing this task" not in basics
            or "spouse" not in basics.split("Before finishing this task")[-1]
        )

    def test_flow_rules_attach_to_firing_task(self) -> None:
        assert "TERMINATION RULE — insurance_not_active" in task("introduction").prompt
        assert "TERMINATION RULE — no_out_of_network_coverage" in task("insurance_basics").prompt
        firing = {t.task_key for t in RENDERED.tasks if "TERMINATION RULE" in t.prompt}
        assert firing == {"introduction", "insurance_basics"}

    def test_contradictions_attach_to_last_field_task(self) -> None:
        assert (
            "CONSISTENCY CHECK — small_group_self_insured_conflict"
            in task("insurance_basics").prompt
        )
        assert (
            "CONSISTENCY CHECK — mandate_requires_infertility_coverage"
            in task("infertility_coverage").prompt
        )

    def test_derive_note_renders(self) -> None:
        basics = task("insurance_basics").prompt
        assert 'record "01/01/{{current_year}}" without asking' in basics

    def test_every_catalog_schema_renders(self) -> None:
        disease = load_document(
            (FORM_SCHEMA_DIR / "disease_only_verification.json").read_text(encoding="utf-8")
        )
        out = render_task_prompts(disease)
        assert out.tasks and all(t.prompt for t in out.tasks)

    def test_no_raw_paths_leak_into_any_prompt(self) -> None:
        for t in RENDERED.tasks:
            assert "sections." not in t.prompt, t.task_key

    def test_multi_gate_or_condition_parenthesized(self) -> None:
        infertility = task("infertility_coverage").prompt
        assert " and (" in infertility
        assert " or " in infertility.split(" and (", 1)[1]

    def test_numeric_range_note_renders(self) -> None:
        infertility = task("infertility_coverage").prompt
        assert "Expected numeric range: 0 to 100." in infertility
        assert "Expected numeric range: at least 0." in infertility

    def test_icd10_codes_render_for_speak_sections(self) -> None:
        assert "ICD-10 Z31.41" in task("diagnostic_coverage").prompt


class TestSnapshots:
    """Golden files lock wording. To update intentionally:
    UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/forms/test_prompting.py -k Snapshots
    then review the diff and commit."""

    def _check(self, name: str, text: str) -> None:
        path = SNAPSHOT_DIR / name
        if os.environ.get("UPDATE_SNAPSHOTS") == "1":
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert text == path.read_text(encoding="utf-8"), f"{name} stale — see docstring"

    def test_introduction_snapshot(self) -> None:
        self._check("ibv_introduction.prompt.txt", task("introduction").prompt)

    def test_insurance_basics_snapshot(self) -> None:
        self._check("ibv_insurance_basics.prompt.txt", task("insurance_basics").prompt)
