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
    drop_questions,
    hydrate_panels,
    iter_questions,
    keep_questions,
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


class TestKeepQuestions:
    """The complement of `drop_questions`: keep the questions a path set still owes, drop the
    rest, and prune any panel left with nothing to ask."""

    def test_a_multi_target_question_is_kept_once_however_many_targets_are_open(self) -> None:
        kept = keep_questions(_OWED_TREE, {"a.covered", "a.copay", "a.coins"})
        assert [q.text for q in iter_questions(kept)] == [
            "Covered, and what copay and coinsurance?"
        ]

    def test_one_open_target_is_enough_to_keep_the_whole_question(self) -> None:
        # The mirror of drop_questions, which keeps a question with even one askable target.
        kept = keep_questions(_OWED_TREE, {"a.copay"})
        assert [q.text for q in iter_questions(kept)] == [
            "Covered, and what copay and coinsurance?"
        ]

    def test_nested_panels_are_reached_and_kept(self) -> None:
        kept = keep_questions(_OWED_TREE, {"a.covered", "a.prior_auth"})
        assert [q.text for q in iter_questions(kept)] == [
            "Covered, and what copay and coinsurance?",
            "Is prior authorization required?",
        ]
        assert [p.title for p in kept[0].children] == ["Prior auth"]

    def test_a_panel_with_nothing_owed_is_pruned(self) -> None:
        kept = keep_questions(_OWED_TREE, {"a.covered"})
        assert kept[0].children == []

    def test_nothing_open_keeps_nothing(self) -> None:
        assert keep_questions(_OWED_TREE, set()) == []

    def test_a_confirm_node_travels_with_its_anchor(self) -> None:
        # The anchor is positional, not modeled: keeping the confirm without the question in
        # front of it would re-anchor the bullet onto whatever lands there next.
        tree = [
            PromptPanel(
                title="Basics",
                items=[
                    PromptQuestion(
                        text="Spouse name?", options=[PromptOption(target_paths=["a.spouse"])]
                    ),
                    PromptQuestion(
                        text="Read back the DOB",
                        options=[PromptOption(target_paths=["a.dob"])],
                        is_confirm=True,
                    ),
                ],
            )
        ]
        assert [q.text for q in iter_questions(keep_questions(tree, {"a.spouse", "a.dob"}))] == [
            "Spouse name?",
            "Read back the DOB",
        ]
        assert [q.text for q in iter_questions(keep_questions(tree, {"a.spouse"}))] == [
            "Spouse name?"
        ]
        # The confirm ALONE no longer vanishes: with its anchor already answered it is promoted
        # to a standalone question, because rendering nothing while the field is still owed lost
        # a required answer outright — see the standalone-promotion test above.
        orphan = keep_questions(tree, {"a.dob"})
        assert [q.text for q in iter_questions(orphan)] == ["Read back the DOB"]

    def test_an_owed_confirm_whose_anchor_is_dropped_stands_alone(self) -> None:
        # A confirm node normally travels with its anchor. But when the anchor is already
        # answered and only the confirm is owed, dropping the run left NOTHING rendered while
        # the guard still counted the field outstanding — silent loss of a required field.
        # Promoted to a standalone question so it numbers, renders and counts.
        tree = [
            PromptPanel(
                title="Basics",
                items=[
                    PromptQuestion(
                        text="Individual or family?",
                        options=[PromptOption(target_paths=["a.coverage_type"])],
                    ),
                    PromptQuestion(
                        text='If "Coverage Type" is "Family": confirm the spouse name',
                        options=[PromptOption(target_paths=["a.spouse"])],
                        is_confirm=True,
                    ),
                ],
            )
        ]
        kept = keep_questions(tree, {"a.spouse"})  # anchor answered, confirm still owed
        assert [q.text for q in iter_questions(kept)] == [
            'If "Coverage Type" is "Family": confirm the spouse name'
        ]
        # Standalone means it takes an ordinal — otherwise the count stays 0 and the gap
        # ceiling it feeds collapses.
        assert numbered_questions(kept) == 1
        assert next(iter_questions(kept)).is_confirm is False

    def test_a_confirm_kept_with_its_anchor_stays_a_nested_bullet(self) -> None:
        tree = [
            PromptPanel(
                title="Basics",
                items=[
                    PromptQuestion(
                        text="Individual or family?",
                        options=[PromptOption(target_paths=["a.coverage_type"])],
                    ),
                    PromptQuestion(
                        text="confirm the spouse name",
                        options=[PromptOption(target_paths=["a.spouse"])],
                        is_confirm=True,
                    ),
                ],
            )
        ]
        kept = keep_questions(tree, {"a.coverage_type", "a.spouse"})
        confirm = next(q for q in iter_questions(kept) if "spouse" in q.text)
        assert confirm.is_confirm is True  # anchor present -> unchanged, unnumbered
        assert numbered_questions(kept) == 1

    def test_a_routing_question_survives_only_while_two_branches_do(self) -> None:
        # It collects nothing, so it can never itself be owed; it earns its place only while
        # there is still a choice to make. With one branch left its own text would name a
        # panel that is no longer below it.
        def tree() -> list[PromptPanel]:
            return [
                PromptPanel(
                    title="Egg cryo",
                    items=[
                        PromptQuestion(text="Elective or cancer?", routes_between=["Elec", "Canc"]),
                        PromptPanel(
                            title="Elec",
                            items=[
                                PromptQuestion(
                                    text="Elective covered?",
                                    options=[PromptOption(target_paths=["a.elec"])],
                                )
                            ],
                        ),
                        PromptPanel(
                            title="Canc",
                            items=[
                                PromptQuestion(
                                    text="Cancer covered?",
                                    options=[PromptOption(target_paths=["a.canc"])],
                                )
                            ],
                        ),
                    ],
                )
            ]

        both = keep_questions(tree(), {"a.elec", "a.canc"})
        assert next(q.text for q in iter_questions(both)) == "Elective or cancer?"
        one = keep_questions(tree(), {"a.elec"})
        assert [q.text for q in iter_questions(one)] == ["Elective covered?"]


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


def test_dropping_the_anchor_drops_its_confirm_run_too() -> None:
    """A confirm node's anchor is positional — whichever question precedes it in the same
    panel's items — not modeled as a reference. Dropping the anchor without dropping the
    confirm run it owns would silently re-anchor those bullets onto whatever question ends
    up next to them instead."""
    doc = build_ibv_standard()
    task = next(t for t in doc.tasks if t.task_key == "insurance_basics")
    panels = build_question_plan(doc, task, _immediate_by_anchor(doc))
    dropped = drop_questions(panels, {"sections.benefit_coverage.coverage_type"})
    questions = list(iter_questions(dropped))
    assert not any(q.is_confirm for q in questions)
    targets = {p for q in questions for p in q.target_paths}
    assert "sections.patient_information.spouse_partner_name" not in targets
    assert "sections.patient_information.spouse_partner_dob" not in targets
