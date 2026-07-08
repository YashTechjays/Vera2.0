"""Instructions for one plan-driven task agent, built from the CallPlan task slice.

Reuses the shared persona + Cartesia readback guide (`prompt.py`); the schema-derived
questions come from the plan, not from a hardcoded script. The agent asks one question at
a time and calls `record_answer` after each caller reply — the runtime (`forms.runtime`)
decides applicability and ordering, so no schema logic lives in the prompt.
"""

from agent_worker.prompt import CARTESIA_MARKUP_GUIDE, SYSTEM_PROMPT
from vera_core.forms.planning import CallPlan, PlanTask

_PROTOCOL = """HOW TO RUN THIS TASK
Ask the questions strictly in the order given, exactly one at a time, and wait for the
representative's answer before moving on — never ask a later question before an earlier one,
and never bundle two. After each answer, call the record_answer tool with just their answer
value; I will tell you the exact next question to ask, or that the task is complete. Only ask
the questions I give you; never invent questions or ask for information not listed. Give a
short warm acknowledgement before each question, but never read the answer back."""


def _question_lines(task: PlanTask) -> str:
    # COLLECT questions plus CONFIRM read-backs (their prompt already carries the value).
    lines = [
        f"{i}. {field.resolved_prompt}"
        for i, field in enumerate(
            (f for f in task.fields if f.status in ("COLLECT", "CONFIRM")), start=1
        )
        if field.resolved_prompt
    ]
    return "\n".join(lines)


def _known_info(plan: CallPlan) -> str:
    """The patient facts already on file (prefilled context), for the agent to answer from
    if the rep asks — never to volunteer."""
    return "\n".join(
        f"- {c.title}: {c.value}" for c in plan.context_knowledge if c.value is not None
    )


def build_plan_task_instructions(plan: CallPlan, task_key: str) -> str:
    """Persona + known patient info + this task's guidance + its question list + the
    ask/record protocol.

    The task intro is NOT included here — the agent speaks it deterministically in
    `on_enter` (like VeraAgent's greeting), so listing it here would voice it twice.
    """
    task = next(t for t in plan.tasks if t.task_key == task_key)
    parts = [SYSTEM_PROMPT]
    if task.prompt:
        parts.append(task.prompt)
    known = _known_info(plan)
    if known:
        parts.append(
            "PATIENT INFO YOU ALREADY KNOW (use to answer the rep if asked; never volunteer)\n"
            + known
        )
    parts.append(f"QUESTIONS FOR THIS TASK\n{_question_lines(task)}")
    parts.append(_PROTOCOL)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)
