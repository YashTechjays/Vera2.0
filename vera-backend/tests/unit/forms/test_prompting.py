"""render_task_prompts: schema document (+ prompt document) → per-task prompt text."""

import logging
import os
import uuid
from pathlib import Path

import pytest

from vera_core.forms.authoring import COVERAGE_CONFIRMATION_RULE
from vera_core.forms.call_plan import (
    CONFIRM_SLOT_RE,
    CallPlan,
    PlanTask,
    compile_call_plan,
    fuse_prefill,
)
from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import Codes, FormSchemaDoc, load_document
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    RenderedPrompts,
    RenderedTaskPrompt,
    SessionBlock,
    TaskTextOverride,
    numbered_questions,
    render_digest,
    render_panels,
    render_task_prompts,
)
from vera_core.forms.question_plan import PromptOption, PromptPanel, PromptQuestion


def _q(text: str, *paths: str) -> PromptQuestion:
    return PromptQuestion(text=text, options=[PromptOption(target_paths=list(paths))])


FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

IBV: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
RENDERED: RenderedPrompts = render_task_prompts(IBV)
# Loaded locally (not imported from test_call_plan) to avoid a circular import:
# test_call_plan.py already imports FORM_SCHEMA_DIR from this module.
PLAN: CallPlan = compile_call_plan(
    IBV, None, schema_version_id=uuid.uuid4(), prompt_version_id=None
)


def task(key: str) -> RenderedTaskPrompt:
    return next(t for t in RENDERED.tasks if t.task_key == key)


def plan_task(plan: CallPlan, key: str) -> PlanTask:
    return next(t for t in plan.tasks if t.task_key == key)


def disease_only_prompts() -> RenderedPrompts:
    doc = load_document(
        (FORM_SCHEMA_DIR / "disease_only_verification.json").read_text(encoding="utf-8")
    )
    return render_task_prompts(doc)


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
        assert "{{confirm:sections.patient_information.spouse_partner_name}}" in basics
        assert (
            "Before finishing this task" not in basics
            or "spouse" not in basics.split("Before finishing this task")[-1]
        )

    def test_flow_rules_attach_to_firing_task(self) -> None:
        assert "TERMINATION RULE — insurance_not_active" in task("introduction").prompt
        assert "TERMINATION RULE — no_out_of_network_coverage" in task("insurance_basics").prompt
        firing = {t.task_key for t in RENDERED.tasks if "TERMINATION RULE" in t.prompt}
        assert firing == {"introduction", "insurance_basics"}

    @pytest.mark.parametrize("insurance_type", sorted(SCHEMAS))
    def test_closing_details_are_asked_only_by_the_task_that_owns_them(
        self, insurance_type: str
    ) -> None:
        """A flow-rule note and a contradiction reason render into the task owning the LAST
        field of their condition, so closing-detail language in one makes an early task ask
        for the representative's name mid-verification."""
        filename, _build = SCHEMAS[insurance_type]
        doc = load_document((FORM_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        owner = doc.section_to_task()[doc.rep_call_reference_number_field.split(".")[1]]
        phrases = ("representative name", "representative's name", "reference number", "rep name")
        for rendered in render_task_prompts(doc).tasks:
            if rendered.task_key == owner:
                continue
            lowered = rendered.prompt.lower()
            assert not any(p in lowered for p in phrases), (insurance_type, rendered.task_key)

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
        out = disease_only_prompts()
        assert out.tasks and all(t.prompt for t in out.tasks)

    def test_no_raw_paths_leak_into_any_prompt(self) -> None:
        # The compiled {{confirm:<path>}} slot deliberately carries the raw path — it
        # is fuse-time-only surface, expanded before the prompt ever reaches the agent.
        for t in RENDERED.tasks:
            assert "sections." not in CONFIRM_SLOT_RE.sub("", t.prompt), t.task_key

    def test_a_covered_gate_reads_as_prose_not_a_field_comparison(self) -> None:
        # Inside a panel `"Covered" is "Yes"` has no antecedent, and on a fanned-out question
        # there is no single field it could name.
        infertility = task("infertility_coverage").prompt
        assert "Ask only if this service is covered." in infertility
        assert 'Ask only if "Covered" is "Yes"' not in infertility

    def test_numeric_bounds_render_on_the_option_line(self) -> None:
        infertility = task("infertility_coverage").prompt
        assert "Coinsurance (%): 0-100" in infertility
        assert "Copay ($): also: $0, None; at least 0" in infertility

    def test_icd10_codes_render_for_speak_sections(self) -> None:
        """Spelled "ICD ten", never "ICD-10": the agent copies this line into what it says, and
        Cartesia voiced the digits as "I-C-D one zero" on a live call. Space, not hyphen, so no
        TTS provider can read the separator aloud as "dash"."""
        prompt = task("diagnostic_coverage").prompt
        assert "ICD ten Z31.41" in prompt
        assert "ICD-10" not in prompt and "ICD-Ten" not in prompt

    def test_no_task_re_explains_the_structure_the_list_already_carries(self) -> None:
        # The old renderer flattened groups/ask_groups away, so every CPT-heavy task carried
        # prose re-describing the grouping and asking for phrasing variety. The panels carry
        # it now, so that prose is gone — and cannot silently come back.
        for rendered in (RENDERED, disease_only_prompts()):
            for t in rendered.tasks:
                assert "vary how you" not in t.prompt.lower()
                assert "That is wording only" not in t.prompt

    def test_coverage_tasks_reject_valid_as_a_coverage_confirmation(self) -> None:
        """A live call took "that code is valid" for a Yes and advanced. "Valid"/"billable"
        describe the code, not the plan's benefit, so every task collecting a covered/not-covered
        answer says so — in both catalog schemas."""
        for key in (
            "infertility_coverage",
            "diagnostic_coverage",
            "general_office_coverage",
            "male_partner",
        ):
            assert COVERAGE_CONFIRMATION_RULE in task(key).prompt, key

        disease = next(t for t in disease_only_prompts().tasks if t.task_key == "disease_coverage")
        assert COVERAGE_CONFIRMATION_RULE in disease.prompt

    def test_the_rule_names_the_closed_set_of_coverage_answers(self) -> None:
        """The fix the second reopen needed: the rule states WHICH three answers exist, so a
        reply outside them is a non-answer however often it is repeated. Pinned separately from
        the presence check above, which compares against the constant and so cannot notice a
        reword that drops the answer set."""
        for answer in ("covered", "not covered", "not applicable"):
            assert answer in COVERAGE_CONFIRMATION_RULE, answer
        assert "exactly three answers" in COVERAGE_CONFIRMATION_RULE
        assert "hearing one twice does not make it one" in COVERAGE_CONFIRMATION_RULE

    def test_the_coverage_rule_stays_off_the_active_coverage_question(self) -> None:
        """`policy_basics` asks whether coverage is "active" — the one place that word IS the
        answer, so the rule listing it as a non-answer must not land there and contradict it."""
        basics = next(t for t in disease_only_prompts().tasks if t.task_key == "policy_basics")
        assert COVERAGE_CONFIRMATION_RULE not in basics.prompt

    def test_cpt_questions_carry_no_answer_instruction(self) -> None:
        # The rendered "- Answers: Yes | No | N/A" line already states the vocabulary.
        for t in RENDERED.tasks:
            for line in t.prompt.splitlines():
                if "CPT code" in line:
                    assert "Please answer" not in line, (t.task_key, line)


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

    def test_fused_insurance_basics_with_spouse_on_file(self) -> None:
        plan = fuse_prefill(
            IBV,
            PLAN,
            {
                "sections.patient_information.spouse_partner_name": "Jane Doe",
                "sections.patient_information.spouse_partner_dob": "1991-04-12",
            },
            current_year=2026,
        )
        self._check(
            "ibv_insurance_basics.fused_with_spouse.prompt.txt",
            plan_task(plan, "insurance_basics").prompt,
        )

    def test_fused_insurance_basics_without_spouse(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "{{" not in text
        self._check("ibv_insurance_basics.fused_without_spouse.prompt.txt", text)


class TestContinuousNumbering:
    """A task's list is numbered once end to end, so its last ordinal IS its question total.
    The voice agent is told that total; if the two ever diverge the prompt lies about the list
    right in front of it."""

    def test_every_real_task_numbers_1_to_n_across_its_sections(self) -> None:
        for task in PLAN.tasks:
            ordinals = [
                int(line.split(".", 1)[0])
                for line in render_panels(task.panels).splitlines()
                if line[:1].isdigit()
            ]
            assert ordinals == list(range(1, len(ordinals) + 1)), task.task_key
            assert numbered_questions(task.panels) == len(ordinals), task.task_key

    def test_a_nested_panel_continues_the_parents_count(self) -> None:
        panels = [
            PromptPanel(
                title="Outer",
                items=[
                    _q("First?", "a.one"),
                    PromptPanel(title="Inner", items=[_q("Second?", "a.two")]),
                    _q("Third?", "a.three"),
                ],
            )
        ]
        rendered = render_panels(panels)
        assert "1. First?" in rendered
        assert "2. Second?" in rendered  # was "1." while numbering restarted per panel
        assert "3. Third?" in rendered
        assert numbered_questions(panels) == 3

    def test_a_routing_question_is_neither_numbered_nor_counted(self) -> None:
        panels = [
            PromptPanel(
                title="Coverage",
                items=[
                    PromptQuestion(text="Individual or family?", routes_between=["Ind", "Fam"]),
                    _q("Spouse name?", "a.spouse"),
                ],
            )
        ]
        assert numbered_questions(panels) == 1
        assert "1. Spouse name?" in render_panels(panels)

    def test_an_empty_tree_counts_nothing(self) -> None:
        assert numbered_questions([]) == 0


class TestStillNeeded:
    """A partially-answered fan-out is one question with some members already on file."""

    def test_still_needed_is_rendered_under_the_question(self) -> None:
        panels = [
            PromptPanel(
                title="Labs",
                items=[
                    PromptQuestion(
                        text="Are codes 58340, 82670 covered?",
                        options=[
                            PromptOption(
                                answers="Yes | No",
                                target_paths=["a.cpt_58340.covered", "a.cpt_82670.covered"],
                            )
                        ],
                        still_needed=["CPT 58340"],
                    )
                ],
            )
        ]
        rendered = render_panels(panels)
        assert "1. Are codes 58340, 82670 covered?" in rendered
        assert "   - Still needed for: CPT 58340." in rendered

    def test_an_unstamped_question_renders_no_such_line(self) -> None:
        panels = [
            PromptPanel(title="Labs", items=[_q("Are codes covered?", "a.cpt_58340.covered")])
        ]
        assert "Still needed" not in render_panels(panels)

    def test_still_needed_does_not_take_an_ordinal(self) -> None:
        panels = [
            PromptPanel(
                items=[
                    PromptQuestion(
                        text="Q",
                        options=[PromptOption(target_paths=["a.x", "a.y"])],
                        still_needed=["CPT 1", "CPT 2"],
                    )
                ]
            )
        ]
        assert numbered_questions(panels) == 1


def _digest_tree() -> list[PromptPanel]:
    """One section panel over two service panels — the real compiled shape."""
    return [
        PromptPanel(
            title="Infertility Treatment",
            items=[
                PromptPanel(
                    title="Ovulation Induction (OI/TI)",
                    codes=Codes(icd10=["Z31.89"]),
                    items=[
                        PromptQuestion(
                            text="What is the cycle limit for ovulation induction?",
                            options=[PromptOption(target_paths=["a.oi.cycle"])],
                            gate_text="this service is covered",
                        )
                    ],
                ),
                PromptPanel(
                    title="IUI",
                    codes=Codes(cpt=["58323", "58322"]),
                    items=[
                        PromptQuestion(
                            text="What is the copay or coinsurance for IUI?",
                            options=[
                                PromptOption(label="Copay ($)", target_paths=["a.iui.copay"]),
                                PromptOption(label="Coinsurance (%)", target_paths=["a.iui.coins"]),
                            ],
                            gate_text="this service is covered",
                        ),
                        PromptQuestion(
                            text="What is the cycle limit for IUI?",
                            options=[PromptOption(target_paths=["a.iui.cycle"])],
                        ),
                    ],
                ),
            ],
        )
    ]


class TestRenderDigest:
    def test_a_crumb_is_printed_once_per_panel_with_its_codes(self) -> None:
        digest = render_digest(_digest_tree())
        assert "Ovulation Induction (OI/TI) [ICD ten Z31.89]:" in digest
        assert "IUI [CPT 58323, 58322]:" in digest
        # The sole root section panel names the task, so it never enters a crumb.
        assert "Infertility Treatment" not in digest
        assert digest.count("IUI [CPT 58323, 58322]:") == 1

    def test_numbering_is_continuous_across_panels(self) -> None:
        digest = render_digest(_digest_tree())
        assert "1. What is the cycle limit for ovulation induction?" in digest
        assert "2. What is the copay or coinsurance for IUI?" in digest
        assert "3. What is the cycle limit for IUI?" in digest

    def test_the_last_ordinal_is_numbered_questions(self) -> None:
        # The refusal's ordinals have to mean the same thing as the list the agent is reading.
        tree = _digest_tree()
        assert f"{numbered_questions(tree)}. What is the cycle limit for IUI?" in render_digest(
            tree
        )

    def test_either_or_labels_and_gate_are_carried_inline(self) -> None:
        assert (
            "What is the copay or coinsurance for IUI? "
            "[either: Copay ($) / Coinsurance (%)] (only if this service is covered)"
        ) in render_digest(_digest_tree())

    def test_still_needed_is_carried_inline(self) -> None:
        panels = [
            PromptPanel(
                title="Labs",
                items=[
                    PromptQuestion(
                        text="Are codes 58340, 82670 covered?",
                        options=[PromptOption(target_paths=["a.cpt_58340.cov", "a.cpt_82670.cov"])],
                        still_needed=["CPT 58340"],
                    )
                ],
            )
        ]
        assert "(still needed for: CPT 58340)" in render_digest(panels)

    def test_a_routing_question_takes_no_ordinal(self) -> None:
        panels = [
            PromptPanel(
                title="Egg cryo",
                items=[
                    PromptQuestion(text="Elective or cancer?", routes_between=["Elec", "Canc"]),
                    _q("Elective covered?", "a.elec"),
                ],
            )
        ]
        digest = render_digest(panels)
        assert "First settle which applies: Elective or cancer?" in digest
        assert "Elec or Canc — only one applies" in digest
        assert "1. Elective covered?" in digest
        assert "1. Elective or cancer?" not in digest

    def test_a_confirm_node_takes_no_ordinal(self) -> None:
        panels = [
            PromptPanel(
                title="Basics",
                items=[
                    _q("Spouse name?", "a.spouse"),
                    PromptQuestion(
                        text="Read back the DOB",
                        options=[PromptOption(target_paths=["a.dob"])],
                        is_confirm=True,
                    ),
                ],
            )
        ]
        digest = render_digest(panels)
        assert "1. Spouse name?" in digest
        assert "Read back the DOB" in digest
        assert "2." not in digest

    def test_an_untitled_panel_yields_lines_with_no_crumb(self) -> None:
        # Hand-built fixtures (and any panel the compiler leaves untitled) still render.
        assert render_digest([PromptPanel(items=[_q("Rep name?", "a.rep")])]) == "1. Rep name?"

    def test_an_empty_tree_renders_empty(self) -> None:
        assert render_digest([]) == ""
