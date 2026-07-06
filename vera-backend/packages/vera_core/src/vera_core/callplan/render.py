"""The call-time prompt pipeline: v2 composite document → the agent's flat
instruction string.

`compile_prompt_document` (vera_core.forms.prompting) produces the schema-derived
half — tasks, sections, questions, gates, codes. This module layers the persona
(persona.py) on top and renders the whole thing into the single instruction string
the worker runs (`CallPlan.flat_instructions`). Prefilled DB values are substituted
in place (confirm read-backs, known-background block).

This consumes `compile_prompt_document`'s output by shape (an untyped
`dict[str, Any]`: `tasks` → `sections` → `questions`, plus `context_fields` /
`shared_conditions`) — the deliberate seam between the schema-derived half
(`forms.prompting`) and this persona/render half; keep the two in step.

Pure and DB-free. M1: one combined prompt for one agent. The structured composite
is carried on the CallPlan for the later per-task-agent milestone.
"""

from collections.abc import Mapping
from typing import Any

from vera_core.callplan.persona import BASE_PERSONA, CARTESIA_MARKUP_GUIDE
from vera_core.schemas import PersonaTweak

_VALUE_PLACEHOLDER = "{{value}}"
_NEUTRAL_VALUE = "the value on file"

_OP_WORDS = {
    "eq": "is",
    "ne": "is not",
    "in": "is one of",
    "not_in": "is not one of",
}


def _leaf_name(field_path: str) -> str:
    """Humanize the last segment of a `sections.a.b.c` path for condition prose."""
    return field_path.rsplit(".", 1)[-1].replace("_", " ")


def _describe_condition(cond: Mapping[str, Any], shared: Mapping[str, Any], depth: int = 0) -> str:
    """Render a v2 condition as short English for a `skip_unless` note. Mirrors the
    frontend `vera-frontend/src/lib/ibv/schema.ts::describeCondition` — keep the
    op words and `all`/`any`/`not`/`ref` shape in sync."""
    if depth > 6:
        return "the relevant condition holds"
    if "ref" in cond:
        target = shared.get(cond["ref"])
        if isinstance(target, Mapping):
            return _describe_condition(target, shared, depth + 1)
        return str(cond["ref"]).replace("_", " ")
    if "all" in cond:
        return " and ".join(_describe_condition(c, shared, depth + 1) for c in cond["all"])
    if "any" in cond:
        return " or ".join(_describe_condition(c, shared, depth + 1) for c in cond["any"])
    if "not" in cond:
        return f"not ({_describe_condition(cond['not'], shared, depth + 1)})"
    if "field" in cond:
        value = cond.get("value")
        value_text = " or ".join(value) if isinstance(value, list) else str(value)
        return (
            f"{_leaf_name(cond['field'])} {_OP_WORDS.get(cond.get('op', 'eq'), 'is')} {value_text}"
        )
    return "the relevant condition holds"


def _resolve_value(question: Mapping[str, Any], prefill: Mapping[str, str]) -> str:
    return prefill.get(question["field_path"]) or question.get("default") or _NEUTRAL_VALUE


def _render_question(
    question: Mapping[str, Any], prefill: Mapping[str, str], shared: Mapping[str, Any]
) -> str:
    text = question.get("question") or question.get("field_path", "")
    if _VALUE_PLACEHOLDER in text:
        text = text.replace(_VALUE_PLACEHOLDER, _resolve_value(question, prefill))
    lines = [f"  - {text}"]

    expected = question.get("expected_values")
    if expected:
        lines.append(f"    Answer: {' / '.join(expected)}")

    # CPT/ICD codes reach the prompt via the authored question text (e.g. "Is CPT
    # code 58323 covered?") and the persona's digit-by-digit pronunciation rule;
    # compile_prompt_document keeps structured `codes` only on group/section nodes,
    # which it does not emit per question, so there is nothing to render here.

    skip_unless = question.get("skip_unless")
    if skip_unless:
        conds = " and ".join(_describe_condition(c, shared) for c in skip_unless)
        lines.append(f"    Only ask this if {conds}; otherwise skip it.")
    return "\n".join(lines)


def _render_section(
    section: Mapping[str, Any], prefill: Mapping[str, str], shared: Mapping[str, Any]
) -> str:
    lines = [f" {section.get('title', section.get('section_key', ''))}:"]
    if section.get("intro"):
        lines.append(f"  {section['intro']}")
    for group in section.get("ask_groups") or []:
        lines.append(f"  - (ask together) {group.get('ask', '')}")
    for question in section.get("questions") or []:
        lines.append(_render_question(question, prefill, shared))
    return "\n".join(lines)


def _render_task(
    task: Mapping[str, Any], index: int, prefill: Mapping[str, str], shared: Mapping[str, Any]
) -> str:
    lines = [f"TASK {index}: {task.get('title', task.get('task_key', ''))}"]
    if task.get("prompt"):
        lines.append(task["prompt"])
    for section in task.get("sections") or []:
        lines.append(_render_section(section, prefill, shared))
    for confirm in task.get("confirm_at_end") or []:
        lines.append(_render_question(confirm, prefill, shared))
    if task.get("outro"):
        lines.append(f"When this task is done, say: {task['outro']}")
    return "\n".join(lines)


def _render_context_block(
    context_fields: list[Mapping[str, Any]], prefill: Mapping[str, str]
) -> str | None:
    """The known-background block: pre-provided data the agent uses as context —
    provide only if the rep asks, never volunteer."""
    known = [c for c in context_fields if prefill.get(c["field_path"])]
    if not known:
        return None
    lines = [
        "KNOWN BACKGROUND (already on file — use as context, "
        "provide only if the rep asks; never volunteer):"
    ]
    lines.extend(f"- {c['title']}: {prefill[c['field_path']]}" for c in known)
    return "\n".join(lines)


def render_runtime_prompt(
    composite: Mapping[str, Any],
    prefill: Mapping[str, str],
    tweak: PersonaTweak | None = None,
) -> str:
    """Render the v2 composite prompt document into the agent's flat instructions:
    persona → known background → each task's questions → tenant extra → TTS guide."""
    parts: list[str] = [BASE_PERSONA]

    context_block = _render_context_block(composite.get("context_fields") or [], prefill)
    if context_block is not None:
        parts.append(context_block)

    shared = composite.get("shared_conditions") or {}
    for index, task in enumerate(composite.get("tasks") or [], start=1):
        parts.append(_render_task(task, index, prefill, shared))

    if tweak is not None and tweak.extra_instructions:
        parts.append(tweak.extra_instructions)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)
