"""Renderer tests over the REAL v2 IBV schema (the code catalog → composite doc).

The renderer turns the schema-derived composite document into the agent's flat
instruction string; these assert the persona layer, task/question rendering,
prefilled confirm/context substitution, and condition prose all land."""

from typing import Any

import pytest

from vera_core.callplan import build_prefill, render_runtime_prompt
from vera_core.callplan.render import _describe_condition
from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.prompting import compile_prompt_document
from vera_core.schemas import PersonaTweak

_PREFILL = build_prefill(
    {
        "sections.patient_information.patient_name": "Jane Doe",
        "sections.patient_information.spouse_partner_name": "John Doe",
        "sections.benefit_coverage.coverage_type": "Family",
        "sections.insurance_information.policy_number": "ABC12345",
    }
)


@pytest.fixture(scope="module")
def composite() -> dict[str, Any]:
    return compile_prompt_document(build_ibv_standard())


@pytest.fixture(scope="module")
def rendered(composite: dict[str, Any]) -> str:
    return render_runtime_prompt(composite, _PREFILL, PersonaTweak())


class TestRender:
    def test_persona_and_tts_guide_present(self, rendered: str) -> None:
        assert "PERSONA" in rendered
        assert "SPOKEN MARKUP" in rendered  # Cartesia guide appended last

    def test_every_task_rendered_in_order(self, composite: dict[str, Any], rendered: str) -> None:
        positions = []
        for i, task in enumerate(composite["tasks"], start=1):
            header = f"TASK {i}: {task['title']}"
            assert header in rendered
            positions.append(rendered.index(header))
        assert positions == sorted(positions)  # tasks in document order

    def test_task_prompt_and_questions_present(
        self, composite: dict[str, Any], rendered: str
    ) -> None:
        first = composite["tasks"][0]
        assert first["prompt"] in rendered
        # a representative question from the first task's first section
        q = first["sections"][0]["questions"][0]["question"]
        assert q in rendered

    def test_cpt_codes_reach_the_prompt(self, rendered: str) -> None:
        # CPT numbers arrive via the authored question text (e.g. "Is CPT code
        # 58323 covered?"); the persona instructs digit-by-digit pronunciation.
        # (Structured group-level `codes` are dropped by compile_prompt_document,
        # so <spell> wrapping only applies to leaf-level codes — none today.)
        assert "58323" in rendered

    def test_confirm_value_substituted(self, rendered: str) -> None:
        # spouse confirm read-back uses the prefilled value, and no {{value}} leaks
        assert "John Doe" in rendered
        assert "{{value}}" not in rendered

    def test_known_background_block(self, rendered: str) -> None:
        assert "KNOWN BACKGROUND" in rendered
        assert "Jane Doe" in rendered  # context field value

    def test_skip_unless_rendered_as_prose(self, rendered: str) -> None:
        assert "Only ask this if" in rendered

    def test_tenant_extra_instructions_included(self, composite: dict[str, Any]) -> None:
        out = render_runtime_prompt(
            composite, _PREFILL, PersonaTweak(extra_instructions="TENANT_EXTRA_LINE")
        )
        assert "TENANT_EXTRA_LINE" in out


class TestDescribeCondition:
    def test_comparison(self) -> None:
        cond = {"field": "sections.benefit_coverage.coverage_type", "op": "eq", "value": "Family"}
        assert _describe_condition(cond, {}) == "coverage type is Family"

    def test_ref_resolves_against_shared(self) -> None:
        shared = {"fam": {"field": "sections.x.coverage_type", "op": "eq", "value": "Family"}}
        assert _describe_condition({"ref": "fam"}, shared) == "coverage type is Family"

    def test_all_any_not(self) -> None:
        a = {"field": "sections.x.a", "op": "eq", "value": "Yes"}
        b = {"field": "sections.x.b", "op": "eq", "value": "No"}
        assert _describe_condition({"all": [a, b]}, {}) == "a is Yes and b is No"
        assert _describe_condition({"any": [a, b]}, {}) == "a is Yes or b is No"
        assert _describe_condition({"not": a}, {}) == "not (a is Yes)"

    def test_not_in_list(self) -> None:
        cond = {"field": "sections.x.t", "op": "not_in", "value": ["A", "B"]}
        assert _describe_condition(cond, {}) == "t is not one of A or B"
