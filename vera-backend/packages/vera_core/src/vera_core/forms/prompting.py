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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import PLACEHOLDER_RE, Condition, FormSchemaDoc, Leaf, Task


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionBlock(_Doc):
    """Session-wide agent text applicable to every task. LITERAL content — consumed
    as-is; nothing underneath is overridden (2026-07-08 spec §4)."""

    persona: str = Field(
        min_length=1,
        description=(
            "Who the agent is: name (VERA), voice/temperament ('calm, professional, "
            "patient'), speech pacing habits, how it refers to itself, pronunciation "
            "tendencies. Vera 1.0's AGENT_PERSONA maps here."
        ),
    )
    goal: str = Field(
        min_length=1,
        description=(
            "What the call is for — e.g. 'verify infertility benefits for a patient "
            "with the payer's representative, completing every applicable question "
            "accurately' — the north star the LLM falls back on when the "
            "conversation drifts."
        ),
    )
    base_instructions: str = Field(
        min_length=1,
        description=(
            "Global behavior rules applied across every task: turn-taking "
            "discipline, value-recording rules ('record exactly what the rep "
            "says', 'never invent an answer'), background-noise/hold handling, "
            "role enforcement ('you ask the questions, don't answer benefits "
            "questions yourself'), anti-repetition, never re-introducing yourself. "
            "Vera 1.0's conversation/value-recording rule blocks map here."
        ),
    )


class TaskTextOverride(_Doc):
    """Sparse patch over one task's schema-authored text; set fields win."""

    intro: str | None = None
    outro: str | None = None
    prompt: str | None = None


class PromptDocument(_Doc):
    """prompt_version.composite_json — literal session block + task text patches."""

    kind: Literal["prompt_document"]
    session: SessionBlock
    task_overrides: dict[str, TaskTextOverride] = Field(default_factory=dict)


# Creation-time content for a schema's very first prompt_version (2026-07-08 spec
# §6.1). Placeholder-free so it is valid for every schema. After bootstrap the DB
# is authoritative — editing these constants never retrofits an existing schema.
FACTORY_SESSION = SessionBlock(
    persona=(
        "You are VERA, an AI virtual assistant calling on behalf of a medical "
        "practice's insurance verification team. You are calm, professional and "
        "patient. You speak clearly at a measured pace, slow down for medical "
        "terms and numbers, and never rush the representative. You refer to "
        "yourself as VERA."
    ),
    goal=(
        "Verify the patient's insurance benefits with the payer's representative, "
        "completing every applicable question on the verification form accurately "
        "and recording each answer exactly as stated."
    ),
    base_instructions=(
        "Ask one question at a time and wait for the answer before moving on. "
        "Record exactly what the representative says — never invent, assume or "
        "round an answer. If an answer is partial or ambiguous, read it back and "
        "ask for confirmation. If the representative asks you to hold, say 'take "
        "your time' once and stay silent until they return. You are the caller "
        "asking the questions: do not answer benefits questions yourself and do "
        "not volunteer information you were not asked for. Do not repeat a "
        "question that has already been answered. Never re-introduce yourself "
        "mid-call. If the representative cannot provide an answer after checking, "
        "note that and move on rather than pressing."
    ),
)


class RenderedTaskPrompt(_Doc):
    task_key: str
    title: str
    intro: str | None = None  # AgentTask entry speech — verbatim
    outro: str | None = None  # AgentTask exit speech — verbatim
    prompt: str  # compiled instruction text


class RenderedPrompts(_Doc):
    name: str
    insurance_type: str
    dsl_version: str
    persona: str  # literal from the session block
    goal: str
    base_instructions: str
    tasks: list[RenderedTaskPrompt]


def validate_prompt_document(doc: PromptDocument, schema_doc: FormSchemaDoc) -> list[str]:
    """Content errors of a prompt document against its pinned schema (spec §4).

    Shape errors are pydantic's job; this checks the parts that need the schema:
    task keys exist, overrides are non-empty, placeholders resolve. The exact
    token `value` is exempt (field-level confirm namespace)."""
    errors: list[str] = []
    valid_tokens = (
        set(schema_doc.system_fields or {})
        | {path for path, leaf in schema_doc.leaf_items() if leaf.role == "context"}
        | {"value"}
    )
    task_keys = {t.task_key for t in schema_doc.tasks}
    texts: list[tuple[str, str | None]] = [
        ("session.persona", doc.session.persona),
        ("session.goal", doc.session.goal),
        ("session.base_instructions", doc.session.base_instructions),
    ]
    for key, override in doc.task_overrides.items():
        if key not in task_keys:
            errors.append(f"task_overrides.{key}: unknown task_key")
        if override.intro is None and override.outro is None and override.prompt is None:
            errors.append(f"task_overrides.{key}: empty override entry")
        texts.extend(
            (f"task_overrides.{key}.{attr}", getattr(override, attr))
            for attr in ("intro", "outro", "prompt")
        )
    for where, text in texts:
        for token in PLACEHOLDER_RE.findall(text or ""):
            if token not in valid_tokens:
                errors.append(f"{where}: unknown placeholder {{{{{token}}}}}")
    return errors


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
