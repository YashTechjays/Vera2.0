"""CallPlan — the runtime projection of one published schema + prompt version.

Compiled once, control-plane side, at call dispatch; stored in Redis and loaded
by the DB-free agent worker at call start. A distilled projection — NOT the full
:class:`FormSchemaDoc` — so the worker never re-runs prompt compilation and
UI-only/intake-only content stays out of the runtime surface:

* prompt text comes from :func:`vera_core.forms.prompting.render_task_prompts`
  (session block + one compiled instruction prompt per task, override-merged);
* per-task field descriptors (the Observer's answer schemas) come from
  :func:`vera_core.forms.conditions.leaf_gates`;
* ``flow_rules`` / ``contradictions`` / ``shared_conditions`` are the dsl models
  verbatim, so :func:`vera_core.forms.conditions.evaluate` works unchanged.

Two stages (the split keeps dispatch-pass memoization honest):

* :func:`compile_call_plan` — the per-SCHEMA-VERSION template: ``{{token}}``
  placeholders stay intact; safe to compile once and reuse across forms.
* :func:`fuse_prefill` — the per-FORM stage: hydrates every resolvable
  ``{{token}}`` with the form's intake-prefilled value (spoken-title fallback
  when no value exists), builds the "Known information" block from prefilled
  context-role leaves, and stamps ``prefilled`` — the answers seed for
  applicability gates and the Phase-2 rule engine. ``{{current_year}}`` is
  hydrated here too; ``{{value}}`` survives as the runtime sentinel.

Note: PHI tokenization was dropped (2026-07-13), so the fused plan carries the
form's raw intake values — the same Redis posture as ``vera:transcript:*`` keys.
"""

import logging
import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import (
    PATH_PREFIX,
    PLACEHOLDER_RE,
    RESERVED_PLACEHOLDER_TOKENS,
    Condition,
    Contradiction,
    FlowRule,
    FormSchemaDoc,
    LeafType,
    RequiredWhen,
    Validation,
)
from vera_core.forms.prompting import PromptDocument, render_task_prompts

logger = logging.getLogger(__name__)

COLLECTABLE_ROLES: frozenset[str] = frozenset({"ask", "confirm"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PlanSession(_Model):
    """Session-wide agent text, literal, applied to every task agent."""

    persona: str
    goal: str
    base_instructions: str


class PlanFieldDescriptor(_Model):
    """One collectable leaf: everything the Observer needs to extract its answer."""

    path: str  # root-anchored; byte-identical to field_answer.field_path
    title: str
    type: LeafType
    role: Literal["ask", "confirm"]
    values: list[str] | None = None
    special_values: list[str] | None = None
    validation: Validation | None = None
    required: bool | RequiredWhen = False
    gates: tuple[Condition, ...] = ()
    inapplicable_value: str | None = None


class PlanTask(_Model):
    """One PlanTaskAgent: compiled instruction text + its collectable fields."""

    task_key: str
    title: str
    intro: str | None = None  # AgentTask entry speech — verbatim
    outro: str | None = None  # AgentTask exit speech — verbatim
    prompt: str  # compiled instruction text
    applicable_when: Condition | None = None
    fields: list[PlanFieldDescriptor] = Field(default_factory=list)


class CallPlan(_Model):
    plan_version: Literal["1"] = "1"
    schema_name: str
    insurance_type: str
    dsl_version: str
    schema_version_id: UUID
    prompt_version_id: UUID | None = None  # None = FACTORY_SESSION fallback ran
    session: PlanSession
    tasks: list[PlanTask]
    flow_rules: list[FlowRule] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    shared_conditions: dict[str, Condition] = Field(default_factory=dict)
    stt_key_terms: list[str] | None = None
    # Per-form stage (fuse_prefill) — empty/None on the compile_call_plan template:
    prefilled: dict[str, Any] = Field(default_factory=dict)  # {path: raw intake value}
    known_information: str | None = None  # "Title: value" lines, context-role leaves only
    on_file_values: str | None = None  # "Title: value" lines, confirm-role prefills (to confirm)


def compile_call_plan(
    doc: FormSchemaDoc,
    prompt_doc: PromptDocument | None,
    *,
    schema_version_id: UUID,
    prompt_version_id: UUID | None,
) -> CallPlan:
    """Compile the per-schema-version CallPlan TEMPLATE (tokens left intact).

    Deterministic: same inputs = identical plan. Task order is document order;
    field order within a task is document order (`leaf_gates`). A confirm leaf
    with `confirm_in_task` belongs to the task it is SPOKEN in, not the task
    owning its (context) section — same routing the prompt renderer uses.
    Run `fuse_prefill` on the result to make the per-form plan.
    """
    rendered = render_task_prompts(doc, prompt_doc)
    section_to_task = doc.section_to_task()
    fields_by_task: dict[str, list[PlanFieldDescriptor]] = {}
    for path, leaf, gates in leaf_gates(doc):
        if leaf.role not in COLLECTABLE_ROLES:
            continue
        cit = leaf.confirm_in_task
        task_key = cit.task_key if cit is not None else section_to_task.get(path.split(".")[1])
        if task_key is None:
            continue
        assert leaf.role == "ask" or leaf.role == "confirm"  # narrow for the Literal
        fields_by_task.setdefault(task_key, []).append(
            PlanFieldDescriptor(
                path=path,
                title=leaf.title,
                type=leaf.type,
                role=leaf.role,
                values=leaf.values,
                special_values=leaf.special_values,
                validation=leaf.validation,
                required=leaf.required,
                gates=gates,
                inapplicable_value=leaf.inapplicable_value,
            )
        )

    tasks = [
        PlanTask(
            task_key=rendered_task.task_key,
            title=rendered_task.title,
            intro=rendered_task.intro,
            outro=rendered_task.outro,
            prompt=rendered_task.prompt,
            applicable_when=task.applicable_when,
            fields=fields_by_task.get(rendered_task.task_key, []),
        )
        for task, rendered_task in zip(doc.tasks, rendered.tasks, strict=True)
    ]

    return CallPlan(
        schema_name=rendered.name,
        insurance_type=rendered.insurance_type,
        dsl_version=rendered.dsl_version,
        schema_version_id=schema_version_id,
        prompt_version_id=prompt_version_id,
        session=PlanSession(
            persona=rendered.persona,
            goal=rendered.goal,
            base_instructions=rendered.base_instructions,
        ),
        tasks=tasks,
        flow_rules=list(doc.flow_rules or []),
        contradictions=list(doc.contradictions or []),
        shared_conditions=dict(doc.shared_conditions or {}),
        stt_key_terms=doc.stt_key_terms,
    )


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# "Dr. Dr. Jane" → "Dr. Jane": a title on both the template ("Dr. {{doctor_name}}")
# and the prefilled value ("Dr. Jane Smith") collapses to a single spoken title.
_DOUBLED_HONORIFIC_RE = re.compile(r"\b(Dr|Mr|Mrs|Ms|Prof)\.(?:\s+\1\.)+", re.IGNORECASE)


def _render_value(raw: Any) -> str | None:
    """Prompt-text rendering of a prefilled raw value; None = not renderable
    (absent, or a shape with no sensible spoken form — dict/None)."""
    if isinstance(raw, str):
        return _speak_iso_date(raw)
    if isinstance(raw, bool | int | float):
        return str(raw)
    if isinstance(raw, list):
        return ", ".join(str(item) for item in raw)
    return None


def _speak_iso_date(text: str) -> str:
    """An ISO ``YYYY-MM-DD`` renders TTS-friendly ("April 12, 1991"); any other
    string (or a non-calendar date) passes through untouched."""
    if not _ISO_DATE_RE.match(text):
        return text
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{parsed:%B} {parsed.day}, {parsed.year}"


def _dedupe_honorifics(text: str) -> str:
    return _DOUBLED_HONORIFIC_RE.sub(lambda m: f"{m.group(1)}.", text)


class PrefillFuser:
    """The per-FORM stage, split so a dispatch pass builds the template-invariant
    lookups ONCE per schema version and calls :meth:`fuse` once per form (the
    dispatch loop runs under the per-tenant advisory lock — per-form document
    walks are wasted serialized time). Holds only the fields it needs, never the
    whole doc."""

    def __init__(self, doc: FormSchemaDoc, plan: CallPlan) -> None:
        self._plan = plan
        self._system = doc.system_fields or {}
        self._titles = {path: field.title for path, field in doc._iter_fields()}
        # The Known-information source set: context-role leaves, document order.
        self._context_leaves = [
            (path, leaf.title) for path, leaf in doc.leaf_items() if leaf.role == "context"
        ]
        # Confirm-role leaves: prefilled values the agent must READ BACK to confirm
        # (their prompt is a {{value}} confirmation). Without this block the agent has
        # no value to confirm and degrades to an open ask (risking a conflicting answer).
        self._confirm_leaves = [
            (path, leaf.title) for path, leaf in doc.leaf_items() if leaf.role == "confirm"
        ]

    def fuse(self, values: Mapping[str, Any], *, current_year: int) -> CallPlan:
        """Fuse one form's intake-prefilled values into the template (pure — the
        template is never mutated).

        * Every resolvable ``{{token}}`` (system_fields key or ``sections.…``
          leaf path) in session + task text becomes the form's actual value;
          with no value it falls back to a neutral spoken reference
          ("the <leaf title>"). Reserved tokens (`RESERVED_PLACEHOLDER_TOKENS`):
          ``{{current_year}}`` hydrates here, ``{{value}}`` stays verbatim.
        * ``known_information`` lists prefilled **context-role** leaves ("Title:
          value" lines) — the schema's "context = agent background" contract.
          Ask/confirm leaves are collected/confirmed on the call and
          input/ui_only leaves are never voice-touched, so they stay out.
        * ``prefilled`` carries the full ``{path: raw}`` map (all roles) — the
          answers seed for applicability gates and the Phase-2 rule engine.
        """
        unresolved = 0

        def hydrate(text: str | None) -> str | None:
            if text is None:
                return None

            def repl(match: re.Match[str]) -> str:
                nonlocal unresolved
                token = match.group(1)
                if token in RESERVED_PLACEHOLDER_TOKENS:
                    return str(current_year) if token == "current_year" else match.group(0)
                path = self._system.get(token, token if token.startswith(PATH_PREFIX) else None)
                if path is not None:
                    rendered = _render_value(values.get(path))
                    if rendered is not None:
                        return rendered
                    title = self._titles.get(path)
                    if title is not None:
                        return f"the {title}"
                unresolved += 1
                return match.group(0)

            return _dedupe_honorifics(PLACEHOLDER_RE.sub(repl, text))

        def value_lines(leaves: list[tuple[str, str]]) -> list[str]:
            return [
                f"{title}: {rendered}"
                for path, title in leaves
                if (rendered := _render_value(values.get(path))) is not None
            ]

        known_lines = value_lines(self._context_leaves)
        on_file_lines = value_lines(self._confirm_leaves)

        plan = self._plan
        fused = plan.model_copy(
            update={
                "session": PlanSession(
                    persona=hydrate(plan.session.persona) or "",
                    goal=hydrate(plan.session.goal) or "",
                    base_instructions=hydrate(plan.session.base_instructions) or "",
                ),
                "tasks": [
                    task.model_copy(
                        update={
                            "intro": hydrate(task.intro),
                            "outro": hydrate(task.outro),
                            "prompt": hydrate(task.prompt) or "",
                        }
                    )
                    for task in plan.tasks
                ],
                "prefilled": dict(values),
                "known_information": "\n".join(known_lines) if known_lines else None,
                "on_file_values": "\n".join(on_file_lines) if on_file_lines else None,
            }
        )
        if unresolved:
            # Count only — prompt text may now carry patient values; never log content.
            logger.warning(
                "call plan %s: %d unresolvable placeholder(s) passed through verbatim",
                plan.insurance_type,
                unresolved,
            )
        return fused


def fuse_prefill(
    doc: FormSchemaDoc,
    plan: CallPlan,
    values: Mapping[str, Any],
    *,
    current_year: int,
) -> CallPlan:
    """One-shot convenience over :class:`PrefillFuser` (single-form callers /
    tests). Multi-form callers should build the fuser once per template."""
    return PrefillFuser(doc, plan).fuse(values, current_year=current_year)
