"""compile_call_plan: schema document (+ prompt document) → the runtime CallPlan
the agent worker builds PlanTaskAgents from. Pure fusion — prompt text comes from
render_task_prompts, field descriptors from leaf_gates; nothing is recompiled here."""

import uuid

from vera_core.forms.call_plan import (
    CallPlan,
    PlanTask,
    _render_value,
    compile_call_plan,
    focus_call_plan,
    fuse_prefill,
)
from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import PLACEHOLDER_RE, FormSchemaDoc, Leaf, load_document
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    SessionBlock,
    TaskTextOverride,
    render_task_prompts,
)

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

    def test_json_round_trip(self) -> None:
        assert CallPlan.model_validate_json(PLAN.model_dump_json()) == PLAN


class TestFocusCallPlan:
    """focus_call_plan narrows a fused plan to a FOCUSED retry (only the given
    fields) — the mechanism that replaced the retry-announcing prompt overlay."""

    def _all_paths(self, plan: CallPlan) -> list[str]:
        return [f.path for t in plan.tasks for f in t.fields]

    def test_keeps_only_requested_fields(self) -> None:
        target = self._all_paths(PLAN)[0]
        focused = focus_call_plan(PLAN, {target})
        assert self._all_paths(focused) == [target]

    def test_drops_tasks_left_empty(self) -> None:
        target = self._all_paths(PLAN)[0]
        focused = focus_call_plan(PLAN, {target})
        assert all(t.fields for t in focused.tasks)
        assert len(focused.tasks) == 1

    def test_clears_on_file_values_but_keeps_persona(self) -> None:
        target = self._all_paths(PLAN)[0]
        focused = focus_call_plan(PLAN, {target})
        assert focused.on_file_values is None
        assert focused.session == PLAN.session
        assert focused.stt_key_terms == PLAN.stt_key_terms

    def test_empty_focus_yields_no_tasks(self) -> None:
        assert focus_call_plan(PLAN, set()).tasks == []

    def test_original_plan_not_mutated(self) -> None:
        before = len(self._all_paths(PLAN))
        focus_call_plan(PLAN, {self._all_paths(PLAN)[0]})
        assert len(self._all_paths(PLAN)) == before
