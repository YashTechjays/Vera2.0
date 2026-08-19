"""Pin that BOTH extractors send every ungated answer-shape rule.

The Observer's live pass and the post-call top-up write into the same `field_answer` column,
and nothing normalizes either one on write. A rule reaching one preamble and not the other is
how that column comes to hold two shapes — the defect `forms.extraction_prompt` exists to
prevent, and the one thing no single-extractor test can catch. It is also why neither side
gates a shape rule: `build_extract_prompt` sees only bare paths, so it *cannot*.

Iterating `UNGATED_ANSWER_SHAPE_RULES` rather than naming the rules keeps this failing closed
— a convention added later is covered here the moment it joins that tuple.
"""

from agent_worker.observer import _extraction_instructions
from control_plane.llm import build_extract_prompt
from vera_core.forms.call_plan import PlanFieldDescriptor, PlanTask
from vera_core.forms.extraction_prompt import UNGATED_ANSWER_SHAPE_RULES

_PATH = "sections.benefit_coverage.plan_year_information"


def test_both_extractors_carry_every_ungated_shape_rule() -> None:
    task = PlanTask(
        task_key="t",
        title="T",
        prompt=".",
        # A `text` leaf and no date field anywhere: the rules ride every task, not just the
        # ones a `type == "date"` gate would have caught.
        fields=[
            PlanFieldDescriptor(path=_PATH, title="Plan Year Information", type="text", role="ask")
        ],
    )
    observer_prompt = _extraction_instructions(task)
    top_up_prompt = build_extract_prompt([_PATH], [])

    assert UNGATED_ANSWER_SHAPE_RULES
    for rule in UNGATED_ANSWER_SHAPE_RULES:
        assert rule in observer_prompt
        assert rule in top_up_prompt
