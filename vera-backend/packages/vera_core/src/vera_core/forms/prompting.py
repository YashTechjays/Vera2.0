"""Prompt compilation: form-schema document → `prompt_version.composite_json`.

Implements the spec §5 prompt-compiler contract as data, not prose: the composite
document nests one JSON object per task, each carrying the task-level `prompt`
from the DSL plus the question list derived from the task's sections — ask/confirm
text, expected vocabulary, hints, codes, and every nuance (gates, requiredness,
defaults, skip-fill values) the runtime needs to phrase and enforce the questions.
Persona/guardrails/IVR/gap-analysis stay in the prompt-pipeline templates (§4.9);
this is the schema-derived half only.

Pure and DB-free; consumed by the seeder (`scripts/seed.py`) and, later, the
call-time prompt pipeline.
"""

from typing import Any

from pydantic import BaseModel

from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import Condition, FormSchemaDoc, Leaf, Task


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _question(path: str, leaf: Leaf, gates: tuple[Condition, ...]) -> dict[str, Any]:
    """One collectable leaf as a prompt question, with its conversational nuances."""
    prompt = leaf.prompt
    # ask/confirm coherence is validator-enforced, so exactly one of these is set.
    question: str | None = None
    if prompt is not None:
        question = prompt.ask if leaf.role == "ask" else prompt.confirm
    q: dict[str, Any] = {
        "field_path": path,
        "role": leaf.role,
        "type": leaf.type,
        "question": question,
    }
    if prompt and prompt.hints:
        q["hints"] = prompt.hints
    if leaf.values:
        q["expected_values"] = leaf.values
    if leaf.special_values:
        q["special_values"] = leaf.special_values
    if isinstance(leaf.required, bool):
        q["required"] = leaf.required
    else:
        q["required"] = {"when": _dump(leaf.required.when)}
    if gates:
        # The full applicability chain (section → groups → leaf), rendered as
        # skip conditions the runtime evaluates against the answer state.
        q["skip_unless"] = [_dump(g) for g in gates]
    if leaf.validation is not None:
        q["validation"] = _dump(leaf.validation)
    for attr in ("default", "inapplicable_value", "tags"):
        value = getattr(leaf, attr)
        if value is not None:
            q[attr] = value
    if leaf.codes is not None:
        q["codes"] = _dump(leaf.codes)
    if leaf.derive is not None:
        q["derive"] = _dump(leaf.derive)
    return q


def _task_entry(
    doc: FormSchemaDoc,
    task: Task,
    questions_by_section: dict[str, list[dict[str, Any]]],
    confirm_at_end: list[dict[str, Any]],
) -> dict[str, Any]:
    entry: dict[str, Any] = {"task_key": task.task_key, "title": task.title}
    for attr in ("prompt", "intro", "outro"):
        value = getattr(task, attr)
        if value is not None:
            entry[attr] = value
    if task.applicable_when is not None:
        entry["applicable_when"] = _dump(task.applicable_when)
    sections: list[dict[str, Any]] = []
    for section_key in task.sections:
        section = doc.sections[section_key]
        section_entry: dict[str, Any] = {"section_key": section_key, "title": section.title}
        if section.prompt is not None:
            section_entry["intro"] = section.prompt.intro
        if section.codes is not None:
            section_entry["codes"] = _dump(section.codes)
        if section.ask_groups:
            section_entry["ask_groups"] = [_dump(g) for g in section.ask_groups]
        if section.alternatives:
            section_entry["alternatives"] = [_dump(a) for a in section.alternatives]
        section_entry["questions"] = questions_by_section.get(section_key, [])
        sections.append(section_entry)
    entry["sections"] = sections
    if confirm_at_end:
        # confirm_in_task leaves (context sections) are spoken at the END of the
        # named task, once their gate answers exist (spec §4.4).
        entry["confirm_at_end"] = confirm_at_end
    return entry


def compile_prompt_document(doc: FormSchemaDoc) -> dict[str, Any]:
    """The composite prompt document: per-task nested JSON of the finalized task
    prompt + schema-derived question lists."""
    questions_by_section: dict[str, list[dict[str, Any]]] = {}
    confirms_by_task: dict[str, list[dict[str, Any]]] = {}
    context_fields: list[dict[str, Any]] = []

    for path, leaf, gates in leaf_gates(doc):
        section_key = path.split(".")[1]
        if leaf.confirm_in_task is not None:
            confirms_by_task.setdefault(leaf.confirm_in_task.task_key, []).append(
                _question(path, leaf, gates)
            )
        elif leaf.role in ("ask", "confirm"):
            questions_by_section.setdefault(section_key, []).append(_question(path, leaf, gates))
        elif leaf.role == "context":
            # The known-background block: injected as "provide if asked, never
            # volunteer"; values come from the form's current answers at call time.
            context_fields.append(
                {"field_path": path, "title": leaf.title, "section_key": section_key}
            )

    composite: dict[str, Any] = {
        "generated_from": "form_schema",
        "dsl_version": doc.dsl_version,
        "name": doc.name,
        "insurance_type": doc.insurance_type,
        "context_fields": context_fields,
        "tasks": [
            _task_entry(doc, task, questions_by_section, confirms_by_task.get(task.task_key, []))
            for task in doc.tasks
        ],
    }
    if doc.shared_conditions:
        composite["shared_conditions"] = {
            name: _dump(cond) for name, cond in doc.shared_conditions.items()
        }
    if doc.flow_rules:
        composite["flow_rules"] = [_dump(rule) for rule in doc.flow_rules]
    if doc.contradictions:
        composite["contradictions"] = [_dump(rule) for rule in doc.contradictions]
    return composite
