"""compile_call_plan: schema document (+ prompt document) → the runtime CallPlan
the agent worker builds PlanTaskAgents from. Pure fusion — prompt text comes from
render_task_prompts, field descriptors from leaf_gates; nothing is recompiled here."""

import uuid
from uuid import uuid4

import pytest

from vera_core.forms.authoring import eq
from vera_core.forms.call_plan import (
    CallPlan,
    PlanFieldDescriptor,
    PlanTask,
    _render_value,
    compile_call_plan,
    focus_call_plan,
    focus_questions,
    fuse_prefill,
    gating_seed,
    owed_now,
)
from vera_core.forms.catalog.disease_only import build_disease_only
from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import (
    PLACEHOLDER_RE,
    AnyCondition,
    FormSchemaDoc,
    Leaf,
    load_document,
)
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    SessionBlock,
    TaskTextOverride,
    numbered_questions,
    render_digest,
    render_panels,
    render_task_prompts,
)
from vera_core.forms.question_plan import PromptOption, PromptPanel, PromptQuestion, iter_questions

from .test_prompting import FORM_SCHEMA_DIR

IBV: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
SCHEMA_VERSION_ID = uuid.uuid4()
PROMPT_VERSION_ID = uuid.uuid4()

PLAN: CallPlan = compile_call_plan(
    IBV,
    None,
    schema_version_id=SCHEMA_VERSION_ID,
    prompt_version_id=None,
)


def spoken_paths(task: PlanTask) -> set[str]:
    """Every path the task's rendered question tree actually asks for."""
    return {p for q in iter_questions(task.panels) for p in q.target_paths}


def plan_task(plan: CallPlan, key: str) -> PlanTask:
    return next(t for t in plan.tasks if t.task_key == key)


def expected_task_for(path: str, leaf: Leaf) -> str | None:
    """Mirror the routing contract: confirm_in_task wins, else the section's task."""
    if leaf.confirm_in_task is not None:
        return leaf.confirm_in_task.task_key
    section_to_task = {s: t.task_key for t in IBV.tasks for s in t.sections}
    return section_to_task.get(path.split(".")[1])


class TestMetadataAndLineage:
    def test_identity_and_lineage(self) -> None:
        assert PLAN.plan_version == "1"
        assert PLAN.schema_name == IBV.name
        assert PLAN.insurance_type == IBV.insurance_type
        assert PLAN.dsl_version == IBV.dsl_version
        assert PLAN.schema_version_id == SCHEMA_VERSION_ID
        assert PLAN.prompt_version_id is None

    def test_prompt_version_lineage_stamped(self) -> None:
        plan = compile_call_plan(
            IBV,
            PromptDocument(kind="prompt_document", session=FACTORY_SESSION),
            schema_version_id=SCHEMA_VERSION_ID,
            prompt_version_id=PROMPT_VERSION_ID,
        )
        assert plan.prompt_version_id == PROMPT_VERSION_ID

    def test_stt_key_terms_pass_through(self) -> None:
        assert PLAN.stt_key_terms == IBV.stt_key_terms


class TestSession:
    def test_factory_fallback_when_no_prompt_doc(self) -> None:
        assert PLAN.session.persona == FACTORY_SESSION.persona
        assert PLAN.session.goal == FACTORY_SESSION.goal
        assert PLAN.session.base_instructions == FACTORY_SESSION.base_instructions

    def test_operator_session_is_literal(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=SessionBlock(persona="P.", goal="G.", base_instructions="B."),
        )
        plan = compile_call_plan(
            IBV, doc, schema_version_id=SCHEMA_VERSION_ID, prompt_version_id=PROMPT_VERSION_ID
        )
        assert (plan.session.persona, plan.session.goal, plan.session.base_instructions) == (
            "P.",
            "G.",
            "B.",
        )


class TestTasks:
    def test_task_order_matches_document(self) -> None:
        assert [t.task_key for t in PLAN.tasks] == [t.task_key for t in IBV.tasks]
        assert [t.title for t in PLAN.tasks] == [t.title for t in IBV.tasks]

    def test_prompt_text_is_rendered_output(self) -> None:
        rendered = render_task_prompts(IBV, None)
        for plan_t, rendered_t in zip(PLAN.tasks, rendered.tasks, strict=True):
            # identical wherever the rendered text carries no placeholders;
            # placeholder-bearing text differs only by neutralization (tested below)
            if "{{" not in rendered_t.prompt:
                assert plan_t.prompt == rendered_t.prompt

    def test_override_merge_flows_through(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"introduction": TaskTextOverride(outro="Onward.")},
        )
        plan = compile_call_plan(
            IBV, doc, schema_version_id=SCHEMA_VERSION_ID, prompt_version_id=PROMPT_VERSION_ID
        )
        assert plan_task(plan, "introduction").outro == "Onward."

    def test_blank_override_survives_compile_and_fuse(self) -> None:
        # A blank override must reach the worker blank, never as the schema default.
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"introduction": TaskTextOverride(intro="", outro="")},
        )
        plan = compile_call_plan(
            IBV, doc, schema_version_id=SCHEMA_VERSION_ID, prompt_version_id=PROMPT_VERSION_ID
        )
        fused = fuse_prefill(IBV, plan, {}, current_year=2026)
        assert (plan_task(fused, "introduction").intro, plan_task(fused, "introduction").outro) == (
            "",
            "",
        )

    def test_applicable_when_carried_from_schema_task(self) -> None:
        for plan_t, doc_t in zip(PLAN.tasks, IBV.tasks, strict=True):
            assert plan_t.applicable_when == doc_t.applicable_when


class TestTemplateKeepsTokens:
    def test_compile_leaves_placeholders_intact(self) -> None:
        """compile_call_plan is the per-schema TEMPLATE stage (memoized at
        dispatch); hydration is fuse_prefill's job, per form."""
        intro = plan_task(PLAN, "introduction").intro
        assert intro is not None and "{{patient_name}}" in intro

    def test_template_carries_no_prefill(self) -> None:
        assert PLAN.prefilled == {}
        assert PLAN.known_information is None


SYSTEM = IBV.system_fields or {}
VALUES: dict[str, object] = {
    SYSTEM["patient_name"]: "Jane Doe",
    SYSTEM["patient_dob"]: "1/2/1990",
    SYSTEM["member_id"]: "ABC123",  # sections.insurance_information.policy_number (confirm)
    SYSTEM["appointment_date"]: "8/2/2026",
    SYSTEM["chart_number"]: "CH-77",  # input role — never voice-touched
}
FUSED: CallPlan = fuse_prefill(IBV, PLAN, VALUES, current_year=2026)


class TestFusePrefill:
    def test_tokens_hydrate_with_real_values(self) -> None:
        intro = plan_task(FUSED, "introduction").intro
        assert intro is not None
        assert "Jane Doe" in intro
        assert "{{patient_name}}" not in intro

    def test_missing_value_falls_back_to_spoken_title(self) -> None:
        bare = fuse_prefill(IBV, PLAN, {}, current_year=2026)
        intro = plan_task(bare, "introduction").intro
        assert intro is not None
        titles = {p: f.title for p, f in IBV._iter_fields()}
        assert f"the {titles[SYSTEM['patient_name']]}" in intro
        assert "{{patient_name}}" not in intro

    def test_current_year_hydrates(self) -> None:
        basics = plan_task(FUSED, "insurance_basics").prompt
        assert "01/01/2026" in basics
        assert "{{current_year}}" not in basics

    def test_only_the_value_sentinel_survives(self) -> None:
        for t in FUSED.tasks:
            for text in (t.intro, t.outro, t.prompt):
                leftover = set(PLACEHOLDER_RE.findall(text or ""))
                assert leftover <= {"value"}, (t.task_key, leftover)

    def test_list_value_renders_comma_joined(self) -> None:
        fused = fuse_prefill(
            IBV, PLAN, {SYSTEM["patient_name"]: ["Jane", "Doe"]}, current_year=2026
        )
        intro = plan_task(fused, "introduction").intro
        assert intro is not None and "Jane, Doe" in intro

    def test_prefilled_map_carried_verbatim(self) -> None:
        assert FUSED.prefilled == VALUES

    def test_known_information_lists_context_role_values_only(self) -> None:
        known = FUSED.known_information
        assert known is not None
        assert "Patient Name: Jane Doe" in known
        assert "Appointment Date: 8/2/2026" in known
        # confirm-role (collected on-call) and input-role (never voice) stay out
        assert "ABC123" not in known
        assert "CH-77" not in known

    def test_no_context_values_means_no_known_information(self) -> None:
        fused = fuse_prefill(IBV, PLAN, {SYSTEM["member_id"]: "ABC123"}, current_year=2026)
        assert fused.known_information is None

    def test_on_file_values_lists_confirm_role_prefills(self) -> None:
        # member_id -> policy_number is a CONFIRM leaf; its prefill must reach the agent
        # so the {{value}} confirm prompt can be spoken (else it degrades to an open ask).
        on_file = FUSED.on_file_values
        assert on_file is not None
        assert "Policy / Member ID: ABC123" in on_file
        # context-role stays in known_information, not here
        assert "Jane Doe" not in on_file
        # input-role (chart_number) is never voice-touched → excluded from both blocks
        assert "CH-77" not in on_file

    def test_no_confirm_values_means_no_on_file_block(self) -> None:
        fused = fuse_prefill(IBV, PLAN, {SYSTEM["patient_name"]: "Jane Doe"}, current_year=2026)
        assert fused.on_file_values is None

    def test_fuse_is_pure_template_not_mutated(self) -> None:
        intro = plan_task(PLAN, "introduction").intro
        assert intro is not None and "{{patient_name}}" in intro

    def test_fused_plan_round_trips(self) -> None:
        assert CallPlan.model_validate_json(FUSED.model_dump_json()) == FUSED


class TestRenderCosmetics:
    def test_render_value_formats_iso_dates_for_speech(self) -> None:
        assert _render_value("1991-04-12") == "April 12, 1991"
        assert _render_value("2026-08-03") == "August 3, 2026"
        # non-ISO strings pass through untouched
        assert _render_value("8/2/2026") == "8/2/2026"
        assert _render_value("not-a-date") == "not-a-date"
        # not a real calendar date → left as-is (never raises)
        assert _render_value("1991-13-99") == "1991-13-99"

    def test_hydration_dedupes_doubled_honorific(self) -> None:
        # template says "Dr. {{doctor_name}}" and the value already carries "Dr." →
        # must collapse to a single "Dr." (the "Dr. Dr. Jane Smith" transcript bug).
        fused = fuse_prefill(
            IBV, PLAN, {SYSTEM["doctor_name"]: "Dr. Jane Smith"}, current_year=2026
        )
        intro = plan_task(fused, "introduction").intro
        assert intro is not None
        assert "Dr. Jane Smith" in intro
        assert "Dr. Dr." not in intro

    def test_iso_dob_renders_friendly_in_known_information(self) -> None:
        fused = fuse_prefill(IBV, PLAN, {SYSTEM["patient_dob"]: "1991-04-12"}, current_year=2026)
        assert fused.known_information is not None
        assert "April 12, 1991" in fused.known_information
        assert "1991-04-12" not in fused.known_information

    @pytest.mark.parametrize("raw", ["N/A", "n/a", " N/A ", "", "   "])
    def test_render_value_drops_placeholder_strings(self, raw: str) -> None:
        assert _render_value(raw) is None

    def test_render_value_keeps_real_values(self) -> None:
        assert _render_value("Jane Doe") == "Jane Doe"
        assert _render_value("1991-04-12") == "April 12, 1991"


class TestFieldDescriptors:
    def test_only_collectable_roles_in_document_order(self) -> None:
        by_task: dict[str, list[str]] = {}
        for path, leaf, _gates in leaf_gates(IBV):
            task_key = expected_task_for(path, leaf)
            if task_key is not None and leaf.role in ("ask", "confirm"):
                by_task.setdefault(task_key, []).append(path)
        for t in PLAN.tasks:
            assert [f.path for f in t.fields] == by_task.get(t.task_key, [])
            assert all(f.role in ("ask", "confirm") for f in t.fields)

    def test_descriptor_carries_answer_schema(self) -> None:
        gates_by_path = {path: gates for path, _leaf, gates in leaf_gates(IBV)}
        enum_desc = next(f for t in PLAN.tasks for f in t.fields if f.type == "enum" and f.values)
        leaf = dict(IBV.leaf_items())[enum_desc.path]
        assert enum_desc.title == leaf.title
        assert enum_desc.values == leaf.values
        assert enum_desc.required == leaf.required
        assert enum_desc.inapplicable_value == leaf.inapplicable_value
        assert list(enum_desc.gates) == list(gates_by_path[enum_desc.path])

    def test_confirm_in_task_bucketed_to_named_task(self) -> None:
        cit_paths = {
            path: leaf.confirm_in_task.task_key
            for path, leaf in IBV.leaf_items()
            if leaf.confirm_in_task is not None
        }
        assert cit_paths, "IBV fixture lost its confirm_in_task leaves?"
        for path, task_key in cit_paths.items():
            assert path in [f.path for f in plan_task(PLAN, task_key).fields]


class TestRulesAndRoundTrip:
    def test_rules_and_shared_conditions_copied_verbatim(self) -> None:
        assert PLAN.flow_rules == (IBV.flow_rules or [])
        assert PLAN.contradictions == (IBV.contradictions or [])
        assert PLAN.shared_conditions == (IBV.shared_conditions or {})

    def test_plan_carries_numeric_consistencies(self) -> None:
        assert [(r.rule_key, r.triplet) for r in PLAN.numeric_consistencies] == [
            ("lifetime_maximum_triplet_consistency", "sections.lifetime_maximum"),
            ("deductible_individual_triplet_consistency", "sections.deductibles.individual"),
            ("deductible_family_triplet_consistency", "sections.deductibles.family"),
            ("oop_individual_triplet_consistency", "sections.out_of_pocket.individual"),
            ("oop_family_triplet_consistency", "sections.out_of_pocket.family"),
        ]

    def test_json_round_trip(self) -> None:
        assert CallPlan.model_validate_json(PLAN.model_dump_json()) == PLAN


class TestFocusCallPlan:
    """focus_call_plan narrows a fused plan to a FOCUSED retry: only what `paths` still
    needs, in both the tracked fields AND the spoken question tree — the mechanism that
    replaced the retry-announcing prompt overlay."""

    def _all_paths(self, plan: CallPlan) -> list[str]:
        return [f.path for t in plan.tasks for f in t.fields]

    def test_keeps_only_requested_fields(self) -> None:
        target = self._all_paths(PLAN)[0]
        focused = focus_call_plan(PLAN, {target}, answers={})
        assert self._all_paths(focused) == [target]

    def test_drops_tasks_left_empty(self) -> None:
        target = self._all_paths(PLAN)[0]
        focused = focus_call_plan(PLAN, {target}, answers={})
        assert all(t.fields for t in focused.tasks)
        assert len(focused.tasks) == 1

    def test_empty_focus_yields_no_tasks(self) -> None:
        assert focus_call_plan(PLAN, set(), answers={}).tasks == []

    def test_narrows_the_question_tree_not_just_the_fields(self) -> None:
        """P7: `focus_call_plan` copied `fields` and left `panels` and `prompt` untouched, so a
        focused retry spoke every question of every surviving task."""
        target = "sections.deductibles.individual.total"
        focused = focus_call_plan(PLAN, {target}, answers={})
        task = plan_task(focused, "financial")
        spoken = spoken_paths(task)
        assert target in spoken
        assert "sections.out_of_pocket.individual.total" not in spoken

    def test_re_renders_the_prompt_from_the_narrowed_tree(self) -> None:
        target = "sections.deductibles.individual.total"
        full = plan_task(PLAN, "financial")
        focused = plan_task(focus_call_plan(PLAN, {target}, answers={}), "financial")
        assert focused.prompt != full.prompt
        assert len(focused.prompt) < len(full.prompt)

    def test_the_reassembly_invariant_survives_narrowing(self) -> None:
        """`PlanTaskAgent._assembled_block` rebuilds the block from these three pieces; if they
        stop agreeing, a narrowed task says something the plan does not carry."""
        focused = focus_call_plan(PLAN, {"sections.deductibles.individual.total"}, answers={})
        for task in focused.tasks:
            if not task.panels:
                continue
            parts = (task.lead_in, render_panels(task.panels), task.trailing)
            assert "\n\n".join(p for p in parts if p) == task.prompt, task.task_key

    def test_fields_and_panels_narrow_to_the_same_set(self) -> None:
        """`owed_now` joins questions against `task.fields`; a question whose fields are missing is
        invisible to the refusal and the gap pass — so the two sets must be EQUAL, not just
        `tracked <= spoken` (that direction holds trivially and misses a spoken-but-untracked
        question, the actual defect)."""
        focused = focus_call_plan(PLAN, {"sections.deductibles.individual.total"}, answers={})
        for task in focused.tasks:
            if not task.panels:
                continue
            spoken = spoken_paths(task)
            tracked = {f.path for f in task.fields}
            assert spoken == tracked, task.task_key

    def test_explode_pulls_in_the_follow_ups_of_an_unanswered_gate_parent(self) -> None:
        """The failure `focus_questions(explode=True)` exists to prevent: the agent asks whether
        infertility treatment is covered, the rep says yes, and — because the Observer extracts in
        a detached pass — nothing is owed yet, so an agent with no sanctioned next question
        invents one."""
        parent = "sections.infertility_treatment.infertility_tx_covered"
        focused = focus_call_plan(PLAN, {parent}, answers={})
        task = plan_task(focused, "infertility_coverage")
        spoken = spoken_paths(task)
        assert parent in spoken
        assert len(spoken) > 1, "the parent's dependents were not pre-loaded"

    def test_an_already_answered_follow_up_is_not_pre_loaded(self) -> None:
        """`_exploded` adds only targets with nothing on file — adding answered ones would make a
        partly-answered fan-out look wholly owed."""
        parent = "sections.infertility_treatment.infertility_tx_covered"
        answered = {
            p: "Yes"
            for p, _leaf in IBV.leaf_items()
            if p.startswith("sections.infertility_treatment.") and p != parent
        }
        focused = focus_call_plan(PLAN, {parent}, answers=answered)
        task = plan_task(focused, "infertility_coverage")
        spoken = spoken_paths(task)
        assert spoken == {parent}

    def test_still_clears_on_file_values_and_keeps_the_session(self) -> None:
        focused = focus_call_plan(PLAN, {"sections.deductibles.individual.total"}, answers={})
        assert focused.on_file_values is None
        assert focused.session == PLAN.session
        assert focused.stt_key_terms == PLAN.stt_key_terms

    def test_original_plan_not_mutated(self) -> None:
        """A path count alone would miss a mutation of `panels` or `prompt` — the two things
        this function now writes — so compare the whole plan's serialized bytes, on the FUSED
        plan (`panels`/`prompt` carry hydrated token text only after fusing)."""
        fused = fuse_prefill(IBV, PLAN, {}, current_year=2026)
        before = fused.model_dump_json()
        focus_call_plan(fused, {self._all_paths(fused)[0]}, answers={})
        assert fused.model_dump_json() == before


SPOUSE_NAME = "sections.patient_information.spouse_partner_name"
SPOUSE_DOB = "sections.patient_information.spouse_partner_dob"
MEMBER_ID = "sections.insurance_information.policy_number"


class TestPanelsMatchThePrompt:
    """The plan carries the prompt twice — as text and as the tree the worker re-renders.
    Anything that transforms one must transform the other, or narrowing a task silently
    changes what it says."""

    def _plan(self) -> CallPlan:
        return compile_call_plan(
            build_ibv_standard(), None, schema_version_id=uuid4(), prompt_version_id=None
        )

    def test_every_task_reassembles_from_its_pieces(self) -> None:
        for task in self._plan().tasks:
            parts = (task.lead_in, render_panels(task.panels), task.trailing)
            assert "\n\n".join(p for p in parts if p) == task.prompt, task.task_key

    def test_every_fused_task_reassembles_from_its_pieces(self) -> None:
        # The FUSED tree is what `_narrowed_block` re-renders, and `fuse_prefill` rewrites
        # `prompt` as one whole string: a spoken string the fuse touches in the text but not
        # in the tree diverges only here, never in the compile-time check above.
        fused = fuse_prefill(
            build_ibv_standard(),
            self._plan(),
            {SPOUSE_NAME: "Jane Doe", MEMBER_ID: "ABC123"},
            current_year=2026,
        )
        for task in fused.tasks:
            parts = (task.lead_in, render_panels(task.panels), task.trailing)
            assert "\n\n".join(p for p in parts if p) == task.prompt, task.task_key

    def test_the_stored_tree_keeps_the_immediate_confirmations(self) -> None:
        # Built once and shared: building it a second time without the confirm nodes gave the
        # worker a tree whose re-render dropped every "Immediately after this answer" block.
        plan = self._plan()
        confirms = sum(q.is_confirm for t in plan.tasks for q in iter_questions(t.panels))
        assert confirms > 0

    def test_fusing_hydrates_the_tree_as_well_as_the_text(self) -> None:
        # A task-entry re-render must not speak a raw {{token}}.
        doc = build_ibv_standard()
        fused = fuse_prefill(doc, self._plan(), {}, current_year=2026)
        for task in fused.tasks:
            assert "{{current_year}}" not in render_panels(task.panels), task.task_key


class TestConfirmSlot:
    def test_expands_to_confirm_when_value_on_file(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_NAME: "Jane Doe"}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "confirm — Can we also check the spouse on the plan?" in text
        assert "I have the spouse listed as Jane Doe" in text
        assert "{{value}}" not in text
        assert "{{confirm:" not in text

    def test_expands_to_ask_when_nothing_on_file(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "ask — Can we also check the spouse on the plan?" in text
        assert "Can I get the spouse's full name?" in text
        assert "{{value}}" not in text
        assert "{{confirm:" not in text

    def test_treats_na_as_nothing_on_file(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_NAME: "N/A"}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "ask — Can we also check the spouse on the plan?" in text

    def test_speaks_iso_date_in_confirm_variant(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_DOB: "1991-04-12"}, current_year=2026)
        assert "April 12, 1991" in plan_task(plan, "insurance_basics").prompt

    def test_focused_retry_still_reads_back_the_value(self) -> None:
        """focus_call_plan clears on_file_values; the value must survive inline."""
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_NAME: "Jane Doe"}, current_year=2026)
        focused = focus_call_plan(plan, [SPOUSE_NAME], answers={SPOUSE_NAME: "Jane Doe"})
        assert focused.on_file_values is None
        assert "Jane Doe" in plan_task(focused, "insurance_basics").prompt

    def test_numbered_question_confirm_branch_is_bare(self) -> None:
        """The numbered-question list never carried the confirm/ask label — only the
        two bullet contexts (immediate/end-of-task) do."""
        plan = fuse_prefill(IBV, PLAN, {MEMBER_ID: "ABC123"}, current_year=2026)
        # matched by content, not by ordinal: the ordinal moves whenever a question is added.
        line = next(
            line
            for line in plan_task(plan, "insurance_basics").prompt.splitlines()
            if "member ID" in line
        )
        assert line.endswith("I have the member ID as ABC123 — can you confirm that is correct?")
        assert "confirm — " not in line


class TestAlternativePairs:
    """The grouping rule itself is covered in test_conditions.py; this asserts only that the
    compiled plan CARRIES it, since the worker is DB-free and cannot derive it."""

    def test_the_plan_carries_per_code_pairs(self) -> None:
        assert PLAN.alternative_pairs
        for pair in PLAN.alternative_pairs:
            assert len({path.rsplit(".", 1)[0] for path in pair}) == 1, pair


class TestDescriptorCarriesDefault:
    """`completion_pct_v2` counts a leaf with a `default` as filled and the export writes it, but
    the descriptor did not carry it — so the bot chased six fields the form already called done."""

    def test_default_reaches_the_descriptor(self) -> None:
        by_path = {f.path: f for task in PLAN.tasks for f in task.fields}
        group_name = by_path["sections.insurance_information.group_name"]
        assert group_name.default == "N/A"
        assert group_name.required is True

    def test_a_leaf_without_a_default_carries_none(self) -> None:
        by_path = {f.path: f for task in PLAN.tasks for f in task.fields}
        assert by_path["sections.insurance_representative.rep_name"].default is None


class TestExclusiveNotes:
    """A routing `alternatives` picks ONE branch but gates none of them, so the Observer inferred
    `No` for the branch not taken — a coverage claim, at confidence 90 on a live call, where `N/A`
    is the truth. The schema has to tell it; it cannot know from the transcript."""

    def _noted(self) -> dict[str, str]:
        return {f.path: f.exclusive_note for t in PLAN.tasks for f in t.fields if f.exclusive_note}

    def test_a_routing_branchs_leaves_name_their_sibling(self) -> None:
        noted = self._noted()
        elective = noted[
            "sections.infertility_treatment.egg_cryopreservation_elective.cpt_89337.covered"
        ]
        assert "Egg Cryopreservation Cancer" in elective
        assert "record N/A here" in elective
        assert "never No" in elective

    def test_the_note_is_symmetric(self) -> None:
        noted = self._noted()
        cancer = noted[
            "sections.infertility_treatment.egg_cryopreservation_cancer.cpt_89337.covered"
        ]
        assert "Egg Cryopreservation Elective" in cancer

    def test_both_asc_branches_are_noted(self) -> None:
        noted = self._noted()
        professional = noted["sections.general_coverage.asc_professional.cpt_58555.covered"]
        assert "ASC Facility" in professional
        assert (
            "ASC Professional Services"
            in (noted["sections.general_coverage.asc_facility.cpt_58555.covered"])
        )

    def test_a_leaf_level_either_or_gets_no_note(self) -> None:
        # cost pairs are alternatives over LEAVES — an either/or over two answers, not a routing
        # choice between services. Answering coinsurance says nothing about applicability.
        noted = self._noted()
        cost = "sections.infertility_treatment.intrauterine_insemination.cpt_58323.copay"
        assert cost not in noted

    def test_an_unrelated_leaf_gets_no_note(self) -> None:
        assert "sections.insurance_representative.rep_name" not in self._noted()


def _plan_field(path: str, title: str, *, required: bool = True) -> PlanFieldDescriptor:
    return PlanFieldDescriptor(path=path, title=title, type="text", role="ask", required=required)


class TestOwedNow:
    """`owed_now` is the tree↔descriptor join the worker's completion guards run on
    (`agent_worker.plan_runtime.PlanRunController.gap_fields` / `owed_question_count`)."""

    @staticmethod
    def _task(panels: list[PromptPanel], fields: list[PlanFieldDescriptor]) -> PlanTask:
        return PlanTask(task_key="t", title="T", prompt="p", panels=panels, fields=fields)

    def test_a_routing_question_is_never_owed_even_if_it_carried_a_target(self) -> None:
        # No real routing question carries a target today (`routes_between` questions collect
        # nothing by construction), but the skip must not depend on that — it is keyed on
        # `routes_between` alone, defensively, so a future builder bug can't resurrect the
        # tree/descriptor disagreement this task exists to remove.
        path = "sections.a.leaf"
        field = _plan_field(path, "Leaf")
        routing = PromptQuestion(
            text="Which applies?",
            routes_between=["Branch A", "Branch B"],
            options=[PromptOption(target_paths=[path])],
        )
        task = self._task([PromptPanel(items=[routing])], [field])
        assert owed_now(task, {}, {}) == []

    def test_a_target_the_field_list_no_longer_carries_is_skipped_not_crashed(self) -> None:
        # `focus_call_plan` narrows `fields` but leaves `panels` untouched, so a focused
        # plan's tree can reference a path no descriptor backs any more.
        kept_path, dropped_path = "sections.a.kept", "sections.a.dropped"
        kept = _plan_field(kept_path, "Kept")
        question = PromptQuestion(
            text="Kept and dropped?",
            options=[PromptOption(target_paths=[kept_path, dropped_path])],
        )
        task = self._task([PromptPanel(items=[question])], [kept])
        assert owed_now(task, {}, {}) == [question]  # `kept` alone owes it; `dropped` never raises

    def test_owed_questions_come_back_in_spoken_order(self) -> None:
        first_path, second_path, third_path = (
            "sections.a.first",
            "sections.a.second",
            "sections.a.third",
        )
        first = PromptQuestion(text="First?", options=[PromptOption(target_paths=[first_path])])
        second = PromptQuestion(text="Second?", options=[PromptOption(target_paths=[second_path])])
        third = PromptQuestion(text="Third?", options=[PromptOption(target_paths=[third_path])])
        fields = [_plan_field(p, p) for p in (first_path, second_path, third_path)]
        task = self._task([PromptPanel(items=[first, second]), PromptPanel(items=[third])], fields)
        # `second` is answered and drops out; `first`/`third` must still come back in the order
        # the tree speaks them, across a panel boundary, not in field or insertion order.
        assert owed_now(task, {second_path: "X"}, {}) == [first, third]


class TestGatingSeed:
    """`ask` is collected ON the call, so a pre-call value for one must not settle a gate."""

    def _fused(self, values: dict[str, object]) -> CallPlan:
        return fuse_prefill(IBV, PLAN, values, current_year=2026)

    def test_ask_role_prefill_is_dropped(self) -> None:
        path = "sections.enrollment.enrollment_required"
        plan = self._fused({path: "N/A"})
        assert plan.prefilled[path] == "N/A"
        assert path not in gating_seed(plan)

    def test_confirm_role_prefill_survives(self) -> None:
        # On file to be read back — the member-ID pattern.
        path = "sections.insurance_information.policy_number"
        plan = self._fused({path: "ABC123"})
        assert gating_seed(plan)[path] == "ABC123"

    def test_context_role_prefill_survives(self) -> None:
        # No task collects it; it is what the clinic supplied.
        path = "sections.patient_information.spouse_gender"
        plan = self._fused({path: "Male"})
        assert gating_seed(plan)[path] == "Male"

    def test_prefilled_itself_is_not_mutated(self) -> None:
        path = "sections.enrollment.enrollment_required"
        plan = self._fused({path: "N/A"})
        gating_seed(plan)
        assert plan.prefilled == {path: "N/A"}

    def test_a_human_typed_ask_value_is_dropped_too(self) -> None:
        """Provenance is not consulted, only role: `field_answer.source` never reaches the
        worker, and the payer's representative is the authority on an ask leaf either way."""
        path = "sections.benefit_coverage.coverage_type"
        plan = self._fused({path: "Family"})
        assert path not in gating_seed(plan)


def _descriptor(plan: CallPlan, suffix: str) -> PlanFieldDescriptor:
    return next(f for t in plan.tasks for f in t.fields if f.path.endswith(suffix))


class TestOwnerTitle:
    """`still_needed` names a fan-out's members by their owning group's title, never by a
    path segment, so it reads correctly on any schema."""

    def test_a_cpt_leaf_is_owned_by_its_cpt_group(self) -> None:
        assert (
            _descriptor(PLAN, "labs_xray_ultrasound.cpt_58340.covered").owner_title == "CPT 58340"
        )

    def test_a_service_level_leaf_is_owned_by_its_service_group(self) -> None:
        field = _descriptor(PLAN, "ovulation_induction.cycle_limit")
        assert field.owner_title == "Ovulation Induction/Timed Intercourse (OI/TI)"

    def test_a_leaf_directly_under_a_section_has_no_owning_group(self) -> None:
        # Only GROUPS own; a section is the panel a question sits under, not a fan-out member.
        assert _descriptor(PLAN, "infertility_treatment.infertility_tx_covered").owner_title is None

    def test_every_descriptor_of_both_catalogs_compiles(self) -> None:
        # disease_only has no ask groups at all; owner_title must still be well-defined.
        for doc in (build_ibv_standard(), build_disease_only()):
            plan = compile_call_plan(doc, None, schema_version_id=uuid4(), prompt_version_id=None)
            for task in plan.tasks:
                for field in task.fields:
                    assert field.owner_title is None or field.owner_title


def _focus_task() -> PlanTask:
    """One service: a 2-code fanned `covered`, plus a copay gated on it."""
    covered = ("s.cpt_1.covered", "s.cpt_2.covered")
    gate = AnyCondition(any=[eq(covered[0], "Yes"), eq(covered[1], "Yes")])
    return PlanTask(
        task_key="t",
        title="T",
        prompt="p",
        fields=[
            PlanFieldDescriptor(
                path=covered[0],
                title="Covered",
                type="enum",
                role="ask",
                required=True,
                owner_title="CPT 1",
            ),
            PlanFieldDescriptor(
                path=covered[1],
                title="Covered",
                type="enum",
                role="ask",
                required=True,
                owner_title="CPT 2",
            ),
            PlanFieldDescriptor(
                path="s.copay",
                title="Copay ($)",
                type="currency",
                role="ask",
                required=True,
                gates=(gate,),
                owner_title="Service",
            ),
        ],
        panels=[
            PromptPanel(
                title="Service",
                items=[
                    PromptQuestion(
                        text="Are codes 1, 2 covered?",
                        options=[PromptOption(target_paths=list(covered))],
                    ),
                    PromptQuestion(
                        text="What is the copay?",
                        options=[PromptOption(target_paths=["s.copay"])],
                        gate_text="this service is covered",
                    ),
                ],
            )
        ],
    )


class TestFocusQuestions:
    def test_it_keeps_only_the_questions_that_answer_the_paths(self) -> None:
        panels = focus_questions(_focus_task(), ["s.copay"], {}, {})
        assert [q.text for q in iter_questions(panels)] == ["What is the copay?"]

    def test_a_fully_owed_fan_out_is_not_stamped(self) -> None:
        panels = focus_questions(_focus_task(), ["s.cpt_1.covered", "s.cpt_2.covered"], {}, {})
        assert next(iter_questions(panels)).still_needed == []

    def test_a_partly_owed_fan_out_names_the_members_it_still_needs(self) -> None:
        panels = focus_questions(_focus_task(), ["s.cpt_2.covered"], {}, {})
        assert next(iter_questions(panels)).still_needed == ["CPT 2"]

    def test_explode_pulls_in_a_question_gated_on_an_owed_path(self) -> None:
        panels = focus_questions(
            _focus_task(), ["s.cpt_1.covered", "s.cpt_2.covered"], {}, {}, explode=True
        )
        assert [q.text for q in iter_questions(panels)] == [
            "Are codes 1, 2 covered?",
            "What is the copay?",
        ]
        # The dependent keeps its own condition, which is what makes it a FOLLOW-UP and not
        # something to ask unconditionally.
        assert next(q for q in iter_questions(panels) if q.text == "What is the copay?").gate_text

    def test_explode_leaves_an_already_answered_dependent_alone(self) -> None:
        panels = focus_questions(
            _focus_task(),
            ["s.cpt_1.covered", "s.cpt_2.covered"],
            {"s.copay": "$30"},
            {},
            explode=True,
        )
        assert [q.text for q in iter_questions(panels)] == ["Are codes 1, 2 covered?"]

    def test_without_explode_the_dependent_stays_out(self) -> None:
        panels = focus_questions(_focus_task(), ["s.cpt_1.covered", "s.cpt_2.covered"], {}, {})
        assert [q.text for q in iter_questions(panels)] == ["Are codes 1, 2 covered?"]

    def test_a_member_with_no_owner_title_suppresses_the_clause(self) -> None:
        # Better to say nothing than to name a member the agent cannot act on.
        task = _focus_task()
        task.fields[1].owner_title = None
        panels = focus_questions(task, ["s.cpt_2.covered"], {}, {})
        assert next(iter_questions(panels)).still_needed == []

    def test_explode_reaches_a_fixpoint_on_the_real_schema(self) -> None:
        task = plan_task(PLAN, "infertility_coverage")
        owed = ["sections.infertility_treatment.embryo_biopsy.cpt_89290.covered"]
        exploded = focus_questions(task, owed, {}, PLAN.shared_conditions, explode=True)
        texts = [q.text for q in iter_questions(exploded)]
        assert any("covered under this plan" in t for t in texts)
        assert any("copay or coinsurance" in t for t in texts)
        assert any("cycle limit" in t for t in texts)
        # Idempotent: exploding the exploded set adds nothing.
        again = focus_questions(
            task,
            [p for q in iter_questions(exploded) for p in q.target_paths],
            {},
            PLAN.shared_conditions,
            explode=True,
        )
        assert [q.text for q in iter_questions(again)] == texts


class TestRealSchemaDigest:
    """The reported defect, against the real documents: seven ambiguous field titles."""

    def test_the_two_cycle_limits_and_two_89337_services_are_distinguishable(self) -> None:
        task = plan_task(PLAN, "infertility_coverage")
        base = "sections.infertility_treatment"
        owed = [
            f"{base}.ovulation_induction.cycle_limit",
            f"{base}.intrauterine_insemination.cycle_limit",
            f"{base}.egg_cryopreservation_elective.cpt_89337.covered",
            f"{base}.egg_cryopreservation_cancer.cpt_89337.covered",
            f"{base}.frozen_embryo_transfer.cpt_58974.covered",
            f"{base}.embryo_biopsy.cpt_89290.covered",
            f"{base}.embryo_biopsy.cpt_89291.covered",
        ]
        panels = focus_questions(task, owed, {}, PLAN.shared_conditions)
        digest = render_digest(panels)
        assert "Ovulation Induction/Timed Intercourse (OI/TI)" in digest
        assert "Intrauterine Insemination (IUI) [CPT 58323, 58322, 89261" in digest
        assert "Egg Cryopreservation Elective [CPT 89337" in digest
        assert "Egg Cryopreservation Cancer [CPT 89337" in digest
        # Seven owed FIELDS, six spoken asks: 89290 and 89291 are one AskGroup question.
        assert numbered_questions(panels) == 6
        assert digest.count("cycle limit") == 2
        # The routing question survives because BOTH egg cryo branches are owed.
        assert "First settle which applies" in digest

    def test_one_egg_cryo_branch_owed_drops_the_routing_question(self) -> None:
        task = plan_task(PLAN, "infertility_coverage")
        owed = ["sections.infertility_treatment.egg_cryopreservation_elective.cpt_89337.covered"]
        digest = render_digest(focus_questions(task, owed, {}, PLAN.shared_conditions))
        assert "Egg Cryopreservation Elective [CPT 89337" in digest
        assert "First settle which applies" not in digest

    def test_a_partly_owed_eight_code_fan_out_names_the_two_it_needs(self) -> None:
        task = plan_task(PLAN, "diagnostic_coverage")
        base = "sections.diagnostic_testing.labs_xray_ultrasound"
        owed = [f"{base}.cpt_58340.covered", f"{base}.cpt_82670.covered"]
        digest = render_digest(focus_questions(task, owed, {}, PLAN.shared_conditions))
        assert "(still needed for: CPT 58340, CPT 82670)" in digest

    def test_a_focused_plan_names_only_members_it_kept_a_descriptor_for(self) -> None:
        # A partly-owed fan-out question survives `keep_questions` WHOLE — it is one spoken
        # sentence over all eight codes, so `focus_call_plan` tracks every one of them as a
        # field even though only one is `wanted`. The still_needed clause must never name a
        # member the plan lacks a descriptor for.
        wanted = "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.covered"
        focused = focus_call_plan(PLAN, [wanted], answers={})
        stamped: list[str] = []
        for task in focused.tasks:
            collectable = {f.owner_title for f in task.fields if f.owner_title}
            render_digest(task.panels)
            for question in iter_questions(task.panels):
                assert set(question.still_needed) <= collectable
                stamped.extend(question.still_needed)
        assert stamped == ["CPT 58340"]

    def test_both_catalogs_narrow_and_render_every_task(self) -> None:
        # disease_only has no ask groups and no routing questions; the narrowing must not
        # assume either exists.
        for doc in (build_ibv_standard(), build_disease_only()):
            plan = compile_call_plan(doc, None, schema_version_id=uuid4(), prompt_version_id=None)
            for task in plan.tasks:
                owed = [field.path for field in task.fields]
                panels = focus_questions(task, owed, {}, plan.shared_conditions)
                # Everything owed means everything kept — a mismatch is a real tree/descriptor
                # disagreement, so investigate it rather than relaxing this.
                assert numbered_questions(panels) == numbered_questions(task.panels), task.task_key
                render_digest(panels)
                focus_questions(task, owed, {}, plan.shared_conditions, explode=True)


class TestReviewFindings:
    """Regressions for the review of the owed-question digest."""

    def test_explode_adds_only_the_members_with_nothing_on_file(self) -> None:
        # A dependent fan-out pulled in WHOLE made `_stamp_still_needed` see owed == targets,
        # so it named none of its members — defeating the clause for its whole reason to exist.
        task = plan_task(PLAN, "diagnostic_coverage")
        base = "sections.diagnostic_testing.labs_xray_ultrasound"
        codes = ("58340", "82670", "83001", "83002", "84146", "84443", "84144", "76830")
        seed = [f"{base}.cpt_{c}.covered" for c in codes]
        on_file = {f"{base}.cpt_{c}.prior_auth": "No" for c in codes[2:]}  # 6 of 8
        panels = focus_questions(task, seed, on_file, PLAN.shared_conditions, explode=True)
        auth = next(q for q in iter_questions(panels) if "prior authorization" in q.text.lower())
        assert auth.still_needed == ["CPT 58340", "CPT 82670"]

    def test_explode_leaves_optional_follow_ups_out_of_the_sweep(self) -> None:
        # The sweep counts its list as required questions; an optional item pads that claim.
        task = plan_task(PLAN, "infertility_coverage")
        base = "sections.infertility_treatment.embryo_biopsy"
        seed = [f"{base}.cpt_89290.covered", f"{base}.cpt_89291.covered"]
        panels = focus_questions(task, seed, {}, PLAN.shared_conditions, explode=True)
        assert [q.text for q in iter_questions(panels) if q.optional] == []

    def test_the_digest_keeps_the_answer_vocabulary(self) -> None:
        # The field-title list carried "(expected one of: …)"; the digest must not lose it.
        task = plan_task(PLAN, "insurance_basics")
        owed = [f.path for f in task.fields if f.values][:1]
        digest = render_digest(focus_questions(task, owed, {}, PLAN.shared_conditions))
        vocab = next(f.values for f in task.fields if f.path == owed[0])
        assert vocab is not None and " | ".join(vocab) in digest

    def test_root_titles_survive_on_a_task_with_several_sections(self) -> None:
        # Suppression is judged on the TASK's shape, not on how many roots survived: financial
        # has 4 root panels and closing_admin 5, so narrowing to one must still name it.
        task = plan_task(PLAN, "financial")
        assert len(task.panels) > 1
        owed = [f.path for f in task.fields if f.path.startswith("sections.lifetime_maximum")]
        panels = focus_questions(task, owed, {}, PLAN.shared_conditions)
        digest = render_digest(panels, task_sections=len(task.panels))
        assert len(panels) == 1  # narrowed to a single surviving root
        assert panels[0].title is not None and panels[0].title in digest
