"""Stage 1 of the prompt compiler: schema structure -> a tree of spoken questions.

The unit under test is the mapping from DSL constructs to *spoken questions*, which is not
one-per-stored-field: an ask group fans one question out over many paths, an alternatives set
turns several paths into labelled options on one question, and groups become panels.
"""

from collections.abc import Iterator

from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.question_plan import (
    PromptPanel,
    PromptQuestion,
    build_question_plan,
    iter_questions,
)

DOC = build_ibv_standard()
TASKS = {t.task_key: t for t in DOC.tasks}


def _plan(task_key: str) -> list[PromptPanel]:
    return build_question_plan(DOC, TASKS[task_key])


def _questions(task_key: str) -> list[PromptQuestion]:
    return list(iter_questions(_plan(task_key)))


def _by_text(task_key: str, needle: str) -> PromptQuestion:
    return next(q for q in _questions(task_key) if needle in q.text)


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
