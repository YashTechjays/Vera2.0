"""Call-plan contract — the compiled, per-call runtime artifact.

`compile_call_plan` (compiler.py) turns a published `SchemaVersion.schema_json`
plus DB prefill into a `CallPlan`: the flattened field list (validation rules,
ask scripts, CPT/ICD metadata, conditional rules, per-field policies) partitioned
per section, with per-section composed prompts. The control plane stashes the
plan in Redis (store.py) and the agent worker consumes it — this module is the
cross-process contract, so models are strict (`extra="forbid"`).

Prefilled DB-known values are carried in the plan as raw values — both in
`PlanField.confirm_value` (structured) and substituted into the composed prompt
strings (`PlanSection.instructions`, `CallPlan.flat_instructions`). PHI
tokenization / sealing was removed (dev simplification), so the plan holds
plaintext prefilled PHI in both places and is synthetic-data-only until a
protection mechanism is reintroduced (see adr/devops-todo.md #8).
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RuleEffect(StrEnum):
    """Normalized `rules[].effect` values (schema uses prose forms)."""

    MAKE_REQUIRED = "make_required"
    TERMINATE_CALL_WHEN = "terminate_call_when"
    ASK_QUESTION = "ask_question"
    AUTO_FILL = "auto_fill"


class RuleCondition(BaseModel):
    """One `rules[].conditions[]` entry: compare another field's answer."""

    model_config = ConfigDict(extra="forbid")

    field: str  # bare field key as authored (not a full field_path)
    comparison: str  # e.g. "is"
    value: str


class FieldRule(BaseModel):
    """A conditional effect on a field (dynamic requiredness, early termination,
    auto-fill, follow-up question). Conditions on prefilled fields are resolved
    at compile time; the rest ride here for the runtime to re-evaluate."""

    model_config = ConfigDict(extra="forbid")

    effect: RuleEffect
    match: str  # "all of these" / "any of these"
    conditions: list[RuleCondition]
    summary: str | None = None


class FieldPolicy(BaseModel):
    """A field-level behavioral checkpoint (e.g. the mandatory `after_answer`
    branch on `facility_inside_network`). `exact_text` is rendered verbatim
    into the owning section's instructions."""

    model_config = ConfigDict(extra="forbid")

    title: str
    verbatim: bool = False
    exact_text: str


class FieldMetadata(BaseModel):
    """Billing-code metadata the agent must be able to speak."""

    model_config = ConfigDict(extra="forbid")

    cpt_codes: list[str] = []
    icd10: str | None = None


class PlanFieldGroup(BaseModel):
    """An object-typed schema field: the ask-context (script, CPT/ICD codes,
    integrity, rules) lives HERE, once, while the leaf data points flatten
    into `PlanField`s that reference it via `group_key`. Avoids duplicating a
    multi-paragraph verbatim script onto every child."""

    model_config = ConfigDict(extra="forbid")

    group_key: str  # the parent's field_path, e.g. "infertility_treatment.ivf"
    title: str
    description: str | None = None
    verbatim_prompt: str | None = None
    ask_prompt: str | None = None
    metadata: FieldMetadata | None = None
    rules: list[FieldRule] = []
    policies: list[FieldPolicy] = []
    group_integrity: str | None = None  # e.g. "all_or_nothing"


class PlanField(BaseModel):
    """One flattened schema field — the FULL definition, not just the confirm
    pair. `field_path` is dot-joined (`section_key.field_key[...]`, recursing
    into object-typed fields); a nested leaf carries its parent's path as
    `group_key` (ask-context on the matching `PlanFieldGroup`). `ui.widget` is
    the one schema key deliberately dropped: form-UI-only, meaningless to the
    voice runtime."""

    model_config = ConfigDict(extra="forbid")

    field_path: str
    title: str
    description: str | None = None
    type: str
    prompt_role: str | None = None  # question | verifiable_question | prose
    required: bool = False  # from required_state == "required"
    enum: list[str] | None = None
    constraint_ref: str | None = None
    verbatim_prompt: str | None = None  # exact ask script, rendered verbatim
    ask_prompt: str | None = None  # prompt.ask
    ask_category: str | None = None  # prompt.category
    metadata: FieldMetadata | None = None
    rules: list[FieldRule] = []
    policies: list[FieldPolicy] = []
    group_integrity: str | None = None  # e.g. "all_or_nothing"
    group_key: str | None = None  # parent PlanFieldGroup.group_key for nested leaves
    mode: Literal["ask", "confirm", "skip"] = "ask"
    confirm_value: str | None = None  # the DB-known value to read back (raw)


class PlanSection(BaseModel):
    """One schema section, compiled: its fields plus the fully composed
    section prompt (`instructions`). `mode`:

    - collect — the agent asks this section's pending fields
    - confirm — every field is prefilled; the agent only reads values back
    - context — never an agent; pre-provided data used as prompt context
    - skip    — fully answered, nothing to confirm; no agent is created
    """

    model_config = ConfigDict(extra="forbid")

    section_key: str
    title: str
    description: str | None = None
    phase_key: str  # phase_order recipe key, e.g. "phase_3_coverage"
    mode: Literal["collect", "confirm", "context", "skip"]
    instructions: str
    fields: list[PlanField]
    groups: list[PlanFieldGroup] = []


class CallPlan(BaseModel):
    """The complete compiled artifact for one call. Serialized to Redis by the
    control plane; deserialized by the worker. Carries raw prefilled values
    (in `confirm_value` AND baked into the instruction strings) —
    synthetic-data-only until PHI protection is reintroduced."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    room_name: str
    tenant_id: str
    call_id: str
    schema_version_id: str
    prompt_version_id: str | None = None
    greeting: str
    flat_instructions: str  # M1 single-agent prompt (M2 moves to per-section)
    sections: list[PlanSection]
