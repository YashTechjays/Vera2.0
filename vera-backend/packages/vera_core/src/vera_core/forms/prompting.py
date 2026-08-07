"""Runtime prompt rendering: `PromptDocument` (session text + sparse per-task text
overrides, `prompt_version.composite_json`) + a published `FormSchemaDoc` → the
task-level instruction prompts the voice agent runs on.

Implements the spec §1/§3 runtime-rendering contract: `PromptDocument` is a thin,
operator-editable literal overlay — session persona/goal/base_instructions plus
any per-task intro/outro/prompt text patches — while every schema-derived nuance
(ask/confirm text, expected vocabulary, hints, codes, gates, requiredness,
defaults, skip-fill values, flow rules, contradictions) is compiled fresh from the
`FormSchemaDoc` at render time via `render_task_prompts`, not stored. The seeder
(`scripts/seed.py`) only ever writes the code-authored `FACTORY_SESSION` on a
schema's first prompt or carries an existing document forward across a schema
republish — it never compiles content.

Pure and DB-free; consumed by the seeder and the call-time prompt pipeline.
"""

import logging
from collections.abc import Callable, Iterator
from itertools import count
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import (
    PLACEHOLDER_RE,
    RESERVED_PLACEHOLDER_TOKENS,
    Codes,
    Condition,
    ConfirmInTask,
    Contradiction,
    FlowRule,
    FormSchemaDoc,
    Leaf,
    Task,
    condition_field_paths,
    malformed_placeholders,
)
from vera_core.forms.prompt_text import build_condition_renderer, confirm_slot
from vera_core.forms.question_plan import PromptPanel, PromptQuestion, build_question_plan


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
    """Sparse patch over one task's schema-authored text; set fields win.

    A blank `intro`/`outro` means "speak nothing there" — distinct from absent,
    which inherits the schema default."""

    intro: str | None = None
    outro: str | None = None
    prompt: str | None = Field(default=None, min_length=1)


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
        "Record exactly what the representative says, and never invent, assume or "
        "round an answer. If an answer is partial or ambiguous, read it back and "
        "ask for confirmation. Keep confirmations short and vary the wording, for "
        "example 'So that's 80% after deductible?' or 'Got it, $1,500, correct?' "
        "or 'That's in-network only?', instead of starting every one with 'Just to "
        "confirm.' If a response is unclear, add a brief natural phrase before "
        "re-asking, such as 'Sorry, I didn't catch that,' 'Could you repeat "
        "that?' or 'Just to make sure I heard that right,' and rotate them so the "
        "same phrase is never used twice in a row. If the representative asks you "
        "to hold, say 'take your time' once and stay silent until they return. "
        "You are the caller asking the questions, so do not answer benefits "
        "questions yourself and do not volunteer information you were not asked "
        "for. Do not repeat a question that has already been answered. Never "
        "re-introduce yourself mid-call. If the representative cannot provide an "
        "answer after checking, note that and move on rather than pressing."
    ),
)


class RenderedTaskPrompt(_Doc):
    task_key: str
    title: str
    intro: str | None = None  # AgentTask entry speech — verbatim
    outro: str | None = None  # AgentTask exit speech — verbatim
    prompt: str  # compiled instruction text: lead_in + the question list + trailing
    # The same text in its three pieces, so a consumer that re-renders the question list
    # (the worker, narrowing against the live gates) reassembles the rest instead of
    # recovering it by splitting the compiled string — which silently dropped the
    # TERMINATION RULE / CONSISTENCY CHECK blocks that follow the list.
    lead_in: str = ""
    panels: list[PromptPanel] = Field(default_factory=list)
    trailing: str = ""


class RenderedPrompts(_Doc):
    name: str
    insurance_type: str
    dsl_version: str
    persona: str  # literal from the session block
    goal: str
    base_instructions: str
    tasks: list[RenderedTaskPrompt]


logger = logging.getLogger(__name__)

_QuestionItem = tuple[str, Leaf, tuple[Condition, ...]]


def _join_gates(gates: tuple[Condition, ...], render_cond: Callable[[Condition], str]) -> str:
    """Join gate conditions with " and ", parenthesizing any individual gate whose
    rendered text already contains " or " — `build_condition_renderer` only wraps a
    ref-to-any in parens when it's nested inside a parent all/any, not at this
    top-level join, so an unparenthesized "A or B and C" would read ambiguously."""
    parts: list[str] = []
    for gate in gates:
        text = render_cond(gate)
        parts.append(f"({text})" if len(gates) > 1 and " or " in text else text)
    return " and ".join(parts)


def render_task_prompts(
    doc: FormSchemaDoc, prompt_doc: PromptDocument | None = None
) -> RenderedPrompts:
    """Session text + one compiled instruction prompt per task (spec §3).

    Deterministic: same doc + same prompt_doc = byte-identical output. `intro`/
    `outro` pass through (override ?? schema default, so a blank override is
    honored) — they are AgentTask entry/exit speech, never folded into the
    instruction text."""
    if prompt_doc is None:
        logger.warning(
            "no prompt document for insurance_type=%s — using factory session text",
            doc.insurance_type,
        )
    session = prompt_doc.session if prompt_doc is not None else FACTORY_SESSION
    overrides = prompt_doc.task_overrides if prompt_doc is not None else {}

    render_cond = build_condition_renderer(doc)
    shared = doc.shared_conditions or {}
    leaves = dict(doc.leaf_items())
    order = {path: i for i, path in enumerate(leaves)}
    section_to_task = doc.section_to_task()
    titles = {path: field.title for path, field in doc._iter_fields()}
    task_titles = {t.task_key: t.title for t in doc.tasks}

    immediate_by_anchor: dict[str, list[_QuestionItem]] = {}
    end_confirms: dict[str, list[_QuestionItem]] = {}
    for path, leaf, gates in leaf_gates(doc):
        cit = leaf.confirm_in_task
        if cit is not None:
            anchor = _anchor(cit, gates, shared, leaves, order, section_to_task)
            if cit.confirm_immediate and anchor is not None:
                immediate_by_anchor.setdefault(anchor, []).append((path, leaf, gates))
            else:
                end_confirms.setdefault(cit.task_key, []).append((path, leaf, gates))

    flow_by_task: dict[str, list[FlowRule]] = {}
    for rule in doc.flow_rules or []:
        key = _last_ref_task(rule.when, shared, leaves, order, section_to_task)
        if key is not None:
            flow_by_task.setdefault(key, []).append(rule)
    contra_by_task: dict[str, list[Contradiction]] = {}
    for contra in doc.contradictions or []:
        key = _last_ref_task(contra.when, shared, leaves, order, section_to_task)
        if key is not None:
            contra_by_task.setdefault(key, []).append(contra)

    tasks_out: list[RenderedTaskPrompt] = []
    for task in doc.tasks:
        override = overrides.get(task.task_key, TaskTextOverride())
        text = _task_text(
            doc,
            task,
            override,
            render_cond,
            immediate_by_anchor,
            end_confirms.get(task.task_key, []),
            flow_by_task.get(task.task_key, []),
            contra_by_task.get(task.task_key, []),
            titles,
            task_titles,
        )
        tasks_out.append(
            RenderedTaskPrompt(
                task_key=task.task_key,
                title=task.title,
                intro=task.intro if override.intro is None else override.intro,
                outro=task.outro if override.outro is None else override.outro,
                prompt=text.prompt,
                lead_in=text.lead_in,
                panels=text.panels,
                trailing=text.trailing,
            )
        )
    return RenderedPrompts(
        name=doc.name,
        insurance_type=doc.insurance_type,
        dsl_version=doc.dsl_version,
        persona=session.persona,
        goal=session.goal,
        base_instructions=session.base_instructions,
        tasks=tasks_out,
    )


def _anchor(
    cit: ConfirmInTask,
    gates: tuple[Condition, ...],
    shared: dict[str, Condition],
    leaves: dict[str, Leaf],
    order: dict[str, int],
    section_to_task: dict[str, str],
) -> str | None:
    """Last document-order collectable leaf in the named task that the gate chain
    references — the question the immediate confirmation attaches to. The
    validator guarantees one exists for confirm_immediate leaves; None routes the
    confirm to the end-of-task block (defense in depth)."""
    if not cit.confirm_immediate:
        return None
    best: str | None = None
    for cond in gates:
        for ref in condition_field_paths(cond, shared):
            leaf = leaves.get(ref)
            if leaf is None or leaf.role not in ("ask", "confirm"):
                continue
            if section_to_task.get(ref.split(".")[1]) != cit.task_key:
                continue
            if best is None or order[ref] > order[best]:
                best = ref
    return best


def _last_ref_task(
    cond: Condition,
    shared: dict[str, Condition],
    leaves: dict[str, Leaf],
    order: dict[str, int],
    section_to_task: dict[str, str],
) -> str | None:
    """The task where a rule can fire: task of the last-answered referenced field."""
    best: tuple[int, str] | None = None
    for ref in condition_field_paths(cond, shared):
        leaf = leaves.get(ref)
        if leaf is None or leaf.role not in ("ask", "confirm"):
            continue
        task_key = section_to_task.get(ref.split(".")[1])
        if task_key is None:
            continue
        if best is None or order[ref] > best[0]:
            best = (order[ref], task_key)
    return best[1] if best else None


def _codes_text(codes: Codes) -> str:
    parts: list[str] = []
    if codes.cpt:
        parts.append(f"CPT {', '.join(codes.cpt)}")
    if codes.icd10:
        parts.append(f"ICD-10 {', '.join(codes.icd10)}")
    return "; ".join(parts)


def render_panels(panels: list[PromptPanel]) -> str:
    """The question list for a set of panels, numbered CONTINUOUSLY across all of them.

    Pure string assembly over an already-resolved tree — every condition was rendered to
    text in `build_question_plan`, so the DB-free worker can call this to re-render the list
    with entry-decided gates applied, and can never render the tree differently than the
    compiler did.

    One counter spans every panel so the last ordinal IS the task's question total. Numbering
    per panel instead made a multi-section task read as several separately finishable lists,
    and a live call ended `insurance_basics` after its first section."""
    numbering = count(1)
    return "\n\n".join(
        "\n".join(_panel_lines(panel, depth=0, numbering=numbering)) for panel in panels
    )


def numbered_questions(panels: list[PromptPanel]) -> int:
    """How many questions `render_panels` gives an ordinal — so also its LAST ordinal.

    Lives here, next to `_panel_lines`, because it describes what that function emits: one
    continuous count across every panel, skipping routing questions. A caller that tells the
    agent "this task has N questions" has to move whenever the numbering does."""
    return sum(
        numbered_questions([item])
        if isinstance(item, PromptPanel)
        else (0 if item.routes_between else 1)
        for panel in panels
        for item in panel.items
    )


def _panel_lines(panel: PromptPanel, depth: int, numbering: Iterator[int]) -> list[str]:
    """One panel: heading, codes, its gate stated once, then its questions.

    `numbering` is the whole task's counter, shared with every sibling and nested panel."""
    lines: list[str] = []
    if panel.title is not None:
        lines.append(f"{'#' * (3 + depth)} {panel.title}")
    if panel.intro:
        lines.append(panel.intro)
    if panel.codes is not None and (codes := _codes_text(panel.codes)):
        speak = (
            "Read these codes aloud when asking"
            if panel.codes.speak_cpt
            else "Provide these codes only if the representative asks"
        )
        lines.append(f"{speak}: {codes}.")
    if panel.gate_text is not None:
        lines.append(f"Ask only if {panel.gate_text}.")
    for item in panel.items:
        if isinstance(item, PromptPanel):
            lines.append("")
            lines.extend(_panel_lines(item, depth + 1, numbering))
            continue
        if item.routes_between:
            # Unnumbered: it routes between the panels below rather than recording an answer
            # of its own — the choice shows up as which panel's Covered is "Yes".
            lines.append(f"Ask first: {item.text}")
            lines.append(
                "Then take only the matching panel below — "
                f"{' or '.join(item.routes_between)} — and skip the other."
            )
            continue
        lines.extend(_numbered_question(next(numbering), item))
    return lines


def _numbered_question(number: int, question: PromptQuestion) -> list[str]:
    lines = [f"{number}. {question.text}"]
    for option in question.options:
        if option.label is None:
            if option.answers:
                lines.append(f"   - Answers: {option.answers}")
        else:
            suffix = f": {option.answers}" if option.answers else ""
            lines.append(f"   - {option.label}{suffix}")
        if option.date_format is not None:
            lines.append(f"   - Expected date format: {option.date_format}")
    for hint in question.hints:
        lines.append(f"   - Hint: {hint}")
    if question.gate_text is not None:
        lines.append(f"   - Ask only if {question.gate_text}.")
    if question.derive_text is not None:
        lines.append(f"   - {question.derive_text}")
    if question.required_text is not None:
        lines.append(f"   - {question.required_text}")
    elif question.optional:
        lines.append("   - Optional; move on if the representative has nothing.")
    if len(question.fanned_codes) > 1:
        lines.append(
            "   - One question for all of these codes; apply the answer to every code the "
            f"representative confirms: {', '.join(question.fanned_codes)}."
        )
    if question.immediate_confirms:
        lines.append("   - Immediately after this answer:")
        lines.extend(f"     * {line}" for line in question.immediate_confirms)
    return lines


class _TaskText(_Doc):
    lead_in: str
    panels: list[PromptPanel]
    trailing: str

    @property
    def prompt(self) -> str:
        return "\n\n".join(
            p for p in (self.lead_in, render_panels(self.panels), self.trailing) if p
        )


def _task_text(
    doc: FormSchemaDoc,
    task: Task,
    override: TaskTextOverride,
    render_cond: Callable[[Condition], str],
    immediate_by_anchor: dict[str, list[_QuestionItem]],
    end_confirms: list[_QuestionItem],
    flow_rules: list[FlowRule],
    contradictions: list[Contradiction],
    titles: dict[str, str],
    task_titles: dict[str, str],
) -> _TaskText:
    lead: list[str] = []
    if task.applicable_when is not None:
        lead.append(f"This task runs only when {render_cond(task.applicable_when)}.")
    instructions = override.prompt or task.prompt
    if instructions:
        lead.append(instructions)

    # The confirm SLOT, not the confirm sentence: which verb and wording this leaf gets
    # depends on whether the form actually prefilled a value, which only `fuse_prefill`
    # knows (`_confirm_slot`).
    confirm_lines = {
        anchor: [
            f"If {_join_gates(gates, render_cond)}: {confirm_slot(cpath)}"
            for cpath, _leaf, gates in items
        ]
        for anchor, items in immediate_by_anchor.items()
    }
    panels = build_question_plan(doc, task, confirm_lines)
    blocks: list[str] = []

    if end_confirms:
        lines = ["Before finishing this task:"]
        for cpath, _leaf, gates in end_confirms:
            only = f" (only if {_join_gates(gates, render_cond)})" if gates else ""
            lines.append(f"- {confirm_slot(cpath)}{only}")
        blocks.append("\n".join(lines))

    for rule in flow_rules:
        target = (
            f' Stop the remaining questions and move to "{task_titles[rule.skip_to_task]}".'
            if rule.skip_to_task is not None
            else " End the call politely."
        )
        note = f" {rule.note}" if rule.note else ""
        blocks.append(
            f"TERMINATION RULE — {rule.rule_key}:\nIf {render_cond(rule.when)}:{note}{target}"
        )
    for contra in contradictions:
        fields = ", ".join(titles.get(p, p) for p in contra.fields)
        clarify = (
            f' Push back once, saying: "{contra.clarify}"'
            if contra.clarify
            else " Push back once and re-clarify."
        )
        blocks.append(
            f"CONSISTENCY CHECK — {contra.rule_key}:\n"
            f"If {render_cond(contra.when)}: {contra.reason}{clarify} "
            f"Then re-confirm: {fields}."
        )
    return _TaskText(lead_in="\n\n".join(lead), panels=panels, trailing="\n\n".join(blocks))


def validate_prompt_document(doc: PromptDocument, schema_doc: FormSchemaDoc) -> list[str]:
    """Content errors of a prompt document against its pinned schema (spec §4).

    Shape errors are pydantic's job; this checks the parts that need the schema:
    task keys exist, no override entry is entirely empty, placeholders resolve.
    Reserved runtime tokens ({{current_year}}) are exempt — the call-plan fuse
    handles them, not field lookup."""
    errors: list[str] = []
    valid_tokens = (
        set(schema_doc.system_fields or {})
        | {path for path, leaf in schema_doc.leaf_items() if leaf.role == "context"}
        | RESERVED_PLACEHOLDER_TOKENS
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
            if token == "value":
                errors.append(
                    f"{where}: {{{{value}}}} is only valid in a schema field's "
                    "prompt.confirm, not in session or task text"
                )
                continue
            if token not in valid_tokens:
                errors.append(f"{where}: unknown placeholder {{{{{token}}}}}")
        for snippet in malformed_placeholders(text or ""):
            errors.append(
                f"{where}: malformed placeholder {snippet!r} "
                "(use {{token}} — word characters and dots only, no spaces)"
            )
    return errors
