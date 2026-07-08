"""Phase 1 — the compiler: a schema document + a call → one immutable Call Plan.

The schema (`FormSchemaDoc`) describes every possible IBV form; this freezes it into
the plan for *one* call — the single artifact the voice runtime reads. The worker never
looks at the schema and never asks a question the plan didn't tell it to.

Pure and DB-free (like `conditions`/`prompting`): the caller passes the already-loaded
document and the current year (sourced from the DB clock, never the app clock). Shares
the `leaf_gates` walk with the seed-time prompt compiler (`prompting.py`); this is the
call-time, typed sibling whose output is serialized to Redis keyed by `room_name`.

`prefill` (field_path → DB value) fills `confirm` fields (read back, `{{value}}` resolved)
and `context` fields (known background). Without a value, `confirm`/`context` compile to
``PENDING_CONTEXT``. Answer-dependent predicates (`applicable_when`, `derive.when`,
`flow_rules`, `contradictions`) are carried but **not evaluated** — that is Phase 2's job.
"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import (
    Condition,
    Contradiction,
    Derive,
    FlowRule,
    FormSchemaDoc,
    Leaf,
    LeafRole,
    Validation,
)

# role + prefill presence → compile-time status (spec "role decides everything"): ask →
# COLLECT; confirm with a value → CONFIRM (read back); confirm/context without one →
# PENDING_CONTEXT. (context values ride on ContextItem, not a PlanField; KNOWN is reserved.)
FieldStatus = Literal["COLLECT", "CONFIRM", "KNOWN", "PENDING_CONTEXT"]

_YEAR_TEMPLATE = "{{current_year}}"


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PlanField(_PlanModel):
    """One collectable leaf, frozen for this call, in ask order within its task."""

    field_path: str
    role: LeafRole
    status: FieldStatus
    resolved_prompt: str | None
    prefilled_value: str | None = None
    # The full applicability chain (section → groups → leaf), carried UNEVALUATED —
    # Phase 2 resolves it against the live answer map right before the field is asked.
    applicable_when: list[Condition] = []
    validation: Validation | None = None
    expected_values: list[str] | None = None
    special_values: list[str] | None = None
    tags: list[str] | None = None
    inapplicable_value: str | None = None
    derive: Derive | None = None


class ContextItem(_PlanModel):
    """A `role=context` field the agent may answer from — value filled once the PHI
    vault lands (deferred), so `value` is None today."""

    field_path: str
    title: str
    value: str | None = None


class PlanTask(_PlanModel):
    task_key: str
    order: int
    intro: str | None = None
    outro: str | None = None
    prompt: str | None = None
    applicable_when: Condition | None = None
    fields: list[PlanField]


class CallPlan(_PlanModel):
    call_id: str
    room_name: str
    schema_version: str
    context_knowledge: list[ContextItem] = []
    tasks: list[PlanTask]
    shared_conditions: dict[str, Condition] | None = None
    flow_rules: list[FlowRule] | None = None
    contradictions: list[Contradiction] | None = None


def _resolve_statics(text: str | None, current_year: int) -> str | None:
    """Substitute date-only templates (`{{current_year}}`). The `{{value}}` placeholder is
    resolved separately in `_plan_field`, from the prefilled DB value."""
    if text is None:
        return None
    return text.replace(_YEAR_TEMPLATE, str(current_year))


def _plan_field(
    path: str,
    leaf: Leaf,
    gates: tuple[Condition, ...],
    current_year: int,
    prefill: Mapping[str, str],
) -> PlanField:
    # ask/confirm coherence is validator-enforced, so exactly one prompt side is set.
    prompt = None
    if leaf.prompt is not None:
        prompt = leaf.prompt.ask if leaf.role == "ask" else leaf.prompt.confirm
    resolved = _resolve_statics(prompt, current_year)
    # ask is always collected live; a confirm with a DB value is read back (its {{value}}
    # placeholder resolved), else it waits (PENDING_CONTEXT).
    status: FieldStatus = "COLLECT" if leaf.role == "ask" else "PENDING_CONTEXT"
    prefilled_value: str | None = None
    value = prefill.get(path)
    if leaf.role == "confirm" and value is not None:
        status = "CONFIRM"
        prefilled_value = value
        if resolved is not None:
            resolved = resolved.replace("{{value}}", value)
    derive = (
        Derive(when=leaf.derive.when, value=_resolve_statics(leaf.derive.value, current_year) or "")
        if leaf.derive is not None
        else None
    )
    return PlanField(
        field_path=path,
        role=leaf.role,
        status=status,
        resolved_prompt=resolved,
        prefilled_value=prefilled_value,
        applicable_when=list(gates),
        validation=leaf.validation,
        expected_values=leaf.values,
        special_values=leaf.special_values,
        tags=leaf.tags,
        inapplicable_value=leaf.inapplicable_value,
        derive=derive,
    )


def compile_call_plan(
    doc: FormSchemaDoc,
    *,
    call_id: str,
    room_name: str,
    current_year: int,
    prefill: Mapping[str, str] = {},
) -> CallPlan:
    """Freeze `doc` into the immutable plan for one call. Steps: pin the version,
    flatten + carry gate chains (via `leaf_gates`), resolve date statics, stamp a status
    per field, and group fields into the schema's ordered tasks. `prefill` (field_path →
    value) fills `confirm` (read-back) and `context` (known-background) fields from the DB."""
    fields_by_section: dict[str, list[PlanField]] = {}
    confirms_by_task: dict[str, list[PlanField]] = {}
    context_knowledge: list[ContextItem] = []

    for path, leaf, gates in leaf_gates(doc):
        if leaf.role == "context":
            context_knowledge.append(
                ContextItem(field_path=path, title=leaf.title, value=prefill.get(path))
            )
        elif leaf.role in ("ask", "confirm"):
            field = _plan_field(path, leaf, gates, current_year, prefill)
            if leaf.confirm_in_task is not None:
                confirms_by_task.setdefault(leaf.confirm_in_task, []).append(field)
            else:
                fields_by_section.setdefault(path.split(".")[1], []).append(field)

    tasks = [
        PlanTask(
            task_key=task.task_key,
            order=order,
            intro=task.intro,
            outro=task.outro,
            prompt=task.prompt,
            applicable_when=task.applicable_when,
            fields=[f for skey in task.sections for f in fields_by_section.get(skey, [])]
            + confirms_by_task.get(task.task_key, []),
        )
        for order, task in enumerate(doc.tasks)
    ]

    return CallPlan(
        call_id=call_id,
        room_name=room_name,
        schema_version=doc.dsl_version,
        context_knowledge=context_knowledge,
        tasks=tasks,
        shared_conditions=doc.shared_conditions,
        flow_rules=doc.flow_rules,
        contradictions=doc.contradictions,
    )
