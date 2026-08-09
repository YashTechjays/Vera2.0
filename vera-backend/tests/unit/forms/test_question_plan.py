"""Stage 1 of the prompt compiler: schema structure -> a tree of spoken questions.

The unit under test is the mapping from DSL constructs to *spoken questions*, which is not
one-per-stored-field: an ask group fans one question out over many paths, an alternatives set
turns several paths into labelled options on one question, and groups become panels.
"""

from collections.abc import Iterator
from typing import Any

from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import immediate_confirms_by_anchor as _immediate_by_anchor
from vera_core.forms.prompting import numbered_questions
from vera_core.forms.question_plan import (
    PromptOption,
    PromptPanel,
    PromptQuestion,
    build_question_plan,
    hydrate_panels,
    iter_questions,
    owed_questions,
)

from .test_schema_dsl import minimal_doc

DOC = build_ibv_standard()
TASKS = {t.task_key: t for t in DOC.tasks}


def _plan(task_key: str) -> list[PromptPanel]:
    return build_question_plan(DOC, TASKS[task_key])


def _questions(task_key: str) -> list[PromptQuestion]:
    return list(iter_questions(_plan(task_key)))


def _by_text(task_key: str, needle: str) -> PromptQuestion:
    return next(q for q in _questions(task_key) if needle in q.text)


def _gated_doc(gate: dict[str, Any]) -> FormSchemaDoc:
    """`minimal_doc` plus a coded service panel whose follow-up question carries `gate`, and
    a context section no task collects."""
    doc = minimal_doc()
    doc["sections"]["intake"] = {
        "title": "Intake",
        "role": "context",
        "fields": {"on_file": {"type": "text", "title": "On File", "role": "context"}},
    }
    doc["sections"]["service"] = {
        "title": "Service",
        "fields": {
            "iui": {
                "type": "group",
                "title": "IUI",
                "codes": {"cpt": ["58323", "58322"]},
                "fields": {
                    "covered": {
                        "type": "enum",
                        "title": "Covered",
                        "role": "ask",
                        "values": ["Yes", "No"],
                        "prompt": {"ask": "Is IUI covered?"},
                    },
                    # Codes the panel above already lists: storage, so its Covered lands in
                    # the same panel and the same scope as the one beside it.
                    "cpt_58322": {
                        "type": "group",
                        "title": "CPT 58322",
                        "codes": {"cpt": ["58322"]},
                        "fields": {
                            "covered": {
                                "type": "enum",
                                "title": "58322 Covered",
                                "role": "ask",
                                "values": ["Yes", "No"],
                                "prompt": {"ask": "Is 58322 covered?"},
                            }
                        },
                    },
                    "cycle_limit": {
                        "type": "text",
                        "title": "Cycle Limit",
                        "role": "ask",
                        "applicable_when": gate,
                        "prompt": {"ask": "What is the cycle limit?"},
                    },
                },
            }
        },
    }
    doc["tasks"].append({"task_key": "coverage", "title": "Coverage", "sections": ["service"]})
    return FormSchemaDoc.model_validate(doc)


def _gated_question(gate: dict[str, Any]) -> PromptQuestion:
    doc = _gated_doc(gate)
    task = next(t for t in doc.tasks if t.task_key == "coverage")
    plan = build_question_plan(doc, task)
    return next(q for q in iter_questions(plan) if "cycle limit" in q.text)


def _panels(task_key: str) -> list[PromptPanel]:
    """Every panel in the tree, depth-first, including nested ones."""

    def walk(panels: list[PromptPanel]) -> Iterator[PromptPanel]:
        for panel in panels:
            yield panel
            yield from walk(panel.children)

    return list(walk(_plan(task_key)))


class TestFanOut:
    def test_an_ask_group_becomes_one_question_over_every_member(self) -> None:
        # diagnostic_testing authors 4 ask groups across its 8 CPT codes; the covered one
        # must collapse 8 stored fields into a single spoken question.
        q = _by_text("diagnostic_coverage", "diagnostic labs, X-ray and ultrasound services")
        assert len(q.target_paths) == 8
        assert all(p.endswith(".covered") for p in q.target_paths)

    def test_the_fanned_codes_are_named_so_the_agent_can_read_them_out(self) -> None:
        q = _by_text("diagnostic_coverage", "diagnostic labs, X-ray and ultrasound services")
        assert q.fanned_codes == [
            "58340",
            "82670",
            "83001",
            "83002",
            "84146",
            "84443",
            "84144",
            "76830",
        ]

    def test_the_task_asks_far_fewer_questions_than_it_stores_fields(self) -> None:
        questions = _questions("diagnostic_coverage")
        targets = {p for q in questions for p in q.target_paths}
        assert len(questions) == 4  # was 33 numbered items
        # 8 codes x 4 sub-fields + the section's own coverage gate
        assert len(targets) == 33


class TestOptions:
    def test_a_cost_pair_becomes_one_question_with_two_labelled_options(self) -> None:
        q = _by_text("general_office_coverage", "copay or coinsurance")
        assert [o.label for o in q.options] == ["Copay ($)", "Coinsurance (%)"]
        assert len(q.target_paths) == 2

    def test_an_option_carries_its_own_answer_shape(self) -> None:
        q = _by_text("general_office_coverage", "copay or coinsurance")
        copay, coinsurance = q.options
        assert "$0" in copay.answers  # special_values
        assert "0" in coinsurance.answers and "100" in coinsurance.answers  # range

    def test_fan_out_and_options_compose(self) -> None:
        # diagnostic copay/coinsurance are BOTH ask groups of 8 AND cost_pairs per code:
        # one question, two options, sixteen targets.
        q = _by_text("diagnostic_coverage", "copay or coinsurance")
        assert [o.label for o in q.options] == ["Copay ($)", "Coinsurance (%)"]
        assert len(q.target_paths) == 16


class TestRoutingQuestions:
    def test_a_group_level_alternatives_becomes_a_routing_question(self) -> None:
        # Never rendered before this change: `Alternatives.ask` was ignored entirely, so the
        # bot asked both ASC panels in full — the same CPT 58555 twice.
        q = _by_text("general_office_coverage", "billed as professional or facility")
        assert q.target_paths == []
        assert q.routes_between == ["ASC Professional Services", "ASC Facility"]

    def test_the_egg_cryopreservation_choice_is_asked_too(self) -> None:
        q = _by_text("infertility_coverage", "covered as elective, or for cancer")
        assert q.target_paths == []


class TestPanels:
    def test_a_treatment_is_one_panel_carrying_all_its_codes(self) -> None:
        iui = next(
            p
            for p in _panels("infertility_coverage")
            if p.title == "Intrauterine Insemination (IUI)"
        )
        assert iui.codes is not None
        assert iui.codes.cpt == ["58323", "58322", "89261"]
        assert iui.codes.icd10 == ["Z31.89"]

    def test_a_per_code_group_never_earns_its_own_heading(self) -> None:
        # `CPT 58323` is a storage node, not something the rep is asked about.
        assert not any(
            p.title and p.title.startswith("CPT ") for p in _panels("infertility_coverage")
        )

    def test_a_service_panel_numbers_its_own_questions_from_one(self) -> None:
        iui = next(
            p
            for p in _panels("infertility_coverage")
            if p.title == "Intrauterine Insemination (IUI)"
        )
        assert len(iui.questions) == 5  # covered / cost / prior auth / cycle limit / notes

    def test_single_code_services_keep_the_service_name_not_the_code(self) -> None:
        titles = [p.title for p in _panels("general_office_coverage") if p.title]
        assert "Office Visits" in titles
        assert "ASC Professional Services" in titles
        assert "ASC Facility" in titles
        assert "CPT 58555" not in titles


class TestGates:
    def test_a_gate_the_runtime_decides_is_never_rendered_as_prose(self) -> None:
        # any_service_requires_prior_auth references only earlier tasks, so the worker
        # resolves it; as prose it was 3,007 chars printed twice.
        assert _by_text("closing_admin", "authorization department").gate_text is None

    def test_a_gate_on_a_field_this_task_asks_survives_as_prose(self) -> None:
        # Nothing at render time can know the answer — the model must evaluate it live.
        q = _by_text("closing_admin", "name and contact phone number of the pharmacy")
        assert q.gate_text == '"PBM Exists" is "Yes"'

    def test_a_panel_gate_is_not_repeated_on_every_question_inside_it(self) -> None:
        iui = next(
            p
            for p in _panels("infertility_coverage")
            if p.title == "Intrauterine Insemination (IUI)"
        )
        # infertility_covered, stated once on the panel and not on the questions inside it
        assert iui.gate_text == '"Infertility Treatment Covered" is "Yes"'
        assert iui.questions[0].gate_text is None

    def test_a_gate_on_a_path_no_task_collects_keeps_its_prose(self) -> None:
        # Task position cannot decide a path nothing collects — an absent context value is
        # unknown, not false — so the prose has to survive. The worker reaches the same
        # verdict on the same path (`PlanRunController._settled`); if only the compiler
        # called it decided, the worker would keep asking a question whose condition it had
        # just dropped from the prompt.
        gate = {"field": "sections.intake.on_file", "op": "eq", "value": "Yes"}
        assert _gated_question(gate).gate_text == '"On File" is "Yes"'


class TestCoveredShortcut:
    """Inside a panel `"Covered" is "Yes"` has no antecedent, so a gate that asks only that is
    reworded — but only where the short wording makes the identical claim."""

    def test_a_single_covered_gate_names_the_service(self) -> None:
        q = _by_text("infertility_coverage", "cycle limit for ovulation induction")
        assert q.gate_text == "this service is covered"

    def test_an_any_over_several_codes_does_not_read_as_all_of_them(self) -> None:
        # IUI's cycle limit is gated on `any` over three CPT codes' Covered; "the codes above
        # are covered" states the other quantifier, and the agent skips a covered service.
        q = _by_text("infertility_coverage", "cycle limit for IUI")
        assert q.gate_text == "any of the codes above is covered"

    def test_an_all_over_several_codes_still_reads_as_all_of_them(self) -> None:
        gate = {
            "all": [
                {"field": "sections.service.iui.covered", "op": "eq", "value": "Yes"},
                {"field": "sections.service.iui.cpt_58322.covered", "op": "eq", "value": "Yes"},
            ]
        }
        assert _gated_question(gate).gate_text == "the codes above are covered"

    def test_an_inequality_is_never_reworded_as_covered(self) -> None:
        # The shortcut inspected paths only, so `covered != "Yes"` — the exact opposite of
        # what it says — was spoken as "this service is covered".
        gate = {"field": "sections.service.iui.covered", "op": "ne", "value": "Yes"}
        assert _gated_question(gate).gate_text == '"Covered" is not "Yes"'

    def test_a_negation_is_never_reworded_as_covered(self) -> None:
        gate = {"not": {"field": "sections.service.iui.covered", "op": "eq", "value": "Yes"}}
        assert _gated_question(gate).gate_text == 'not ("Covered" is "Yes")'


class TestHydration:
    def test_every_spoken_string_in_the_tree_is_hydrated(self) -> None:
        # `fuse_prefill` rewrites a task's text as ONE string, so any slot missed here
        # hydrates in `prompt` and stays raw in `panels` — the tree the worker re-renders.
        tree = [
            PromptPanel(
                title="{{tok}} title",
                intro="{{tok}} intro",
                gate_text="{{tok}} panel gate",
                items=[
                    PromptQuestion(
                        text="{{tok}} text",
                        options=[PromptOption(label="{{tok}} label", answers="{{tok}} answers")],
                        gate_text="{{tok}} gate",
                        derive_text="{{tok}} derive",
                        required_text="{{tok}} required",
                        is_confirm=True,
                        confirm_line="{{tok}} confirm",
                        hints=["{{tok}} hint"],
                    ),
                    PromptPanel(
                        title="{{tok}} child",
                        items=[
                            PromptQuestion(text="{{tok}} route", routes_between=["{{tok}} between"])
                        ],
                    ),
                ],
            )
        ]
        hydrated = hydrate_panels(tree, lambda s: s.replace("{{tok}}", "Jane"))
        assert "{{tok}}" not in "".join(panel.model_dump_json() for panel in hydrated)


_OWED_TREE = [
    PromptPanel(
        title="CPT 58340",
        items=[
            PromptQuestion(
                text="Covered, and what copay and coinsurance?",
                options=[PromptOption(target_paths=["a.covered", "a.copay", "a.coins"])],
            ),
            PromptPanel(
                title="Prior auth",
                items=[
                    PromptQuestion(
                        text="Is prior authorization required?",
                        options=[PromptOption(target_paths=["a.prior_auth"])],
                    )
                ],
            ),
        ],
    )
]


class TestOwedQuestions:
    """The complement of `drop_questions`, and the unit both completion guards count in."""

    def test_a_multi_target_question_is_owed_once_however_many_targets_are_open(self) -> None:
        assert len(owed_questions(_OWED_TREE, {"a.covered", "a.copay", "a.coins"})) == 1

    def test_one_open_target_is_enough_to_owe_the_whole_question(self) -> None:
        # The mirror of drop_questions, which keeps a question with even one askable target.
        assert len(owed_questions(_OWED_TREE, {"a.copay"})) == 1

    def test_nested_panels_are_reached(self) -> None:
        owed = owed_questions(_OWED_TREE, {"a.covered", "a.prior_auth"})
        assert [q.text for q in owed] == [
            "Covered, and what copay and coinsurance?",
            "Is prior authorization required?",
        ]

    def test_nothing_open_owes_nothing(self) -> None:
        assert owed_questions(_OWED_TREE, set()) == []

    def test_a_routing_question_is_never_owed(self) -> None:
        # It has no target_paths — it chooses between panels rather than collecting anything,
        # so counting it would inflate the ceiling by one ask that answers no field.
        tree = [
            PromptPanel(
                title="Coverage",
                items=[
                    PromptQuestion(text="Individual or family?", routes_between=["Ind", "Fam"]),
                    PromptQuestion(
                        text="Spouse name?",
                        options=[PromptOption(target_paths=["a.spouse"])],
                    ),
                ],
            )
        ]
        assert [q.text for q in owed_questions(tree, {"a.spouse"})] == ["Spouse name?"]


class TestCoverage:
    def test_every_collectable_field_of_a_task_is_reachable_from_some_question(self) -> None:
        # The compiler may regroup questions but must never silently drop a field.
        for task in DOC.tasks:
            collectable = {
                p
                for p in DOC.collection_paths(task.sections)
                if DOC.sections[p.split(".")[1]].role == "collect"
            }
            asked = {
                p for q in iter_questions(build_question_plan(DOC, task)) for p in q.target_paths
            }
            assert collectable <= asked, f"{task.task_key} drops {sorted(collectable - asked)}"


def test_confirm_anchors_are_reachable_question_nodes() -> None:
    """A confirm_immediate leaf must be a node with target_paths, not prose on its anchor.

    Anything that walks the tree to decide what is still owed can only see nodes; a
    pre-rendered string is invisible to it (spec §1).
    """
    doc = build_ibv_standard()
    task = next(t for t in doc.tasks if t.task_key == "insurance_basics")
    panels = build_question_plan(doc, task, _immediate_by_anchor(doc))
    reachable = {p for q in iter_questions(panels) for p in q.target_paths}
    assert "sections.patient_information.spouse_partner_name" in reachable
    assert "sections.patient_information.spouse_partner_dob" in reachable


def test_confirm_nodes_are_not_numbered() -> None:
    """They render nested under their anchor, so they must not consume an ordinal —
    the same treatment routing questions already get."""
    doc = build_ibv_standard()
    task = next(t for t in doc.tasks if t.task_key == "insurance_basics")
    panels = build_question_plan(doc, task, _immediate_by_anchor(doc))
    assert numbered_questions(panels) == 16
