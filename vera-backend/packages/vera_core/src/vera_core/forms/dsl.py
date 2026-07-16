"""Form-schema DSL v2.1 — typed contract, document validator, and compiler.

One set of pydantic models plays three roles (see
docs/superpowers/specs/2026-07-02-form-schema-dsl-v2-design.md):

* **Contract** — every consumer parses ``schema_version.schema_json`` through
  :class:`FormSchemaDoc` instead of hand-walking dicts.
* **Authoring DSL** — schemas are written in Python against these models (with the
  macros in :mod:`vera_core.forms.authoring`); the models *are* the grammar.
* **Compiler** — :func:`compile_document` emits the canonical JSON artifact stored in
  ``data/form_schemas/`` and seeded to the DB. :func:`load_document` round-trips it.

The compiled artifact is deliberately explicit and self-contained: every leaf carries
a ``role``, every reference is a root-anchored ``sections.…`` path, and reuse exists
only via ``shared_conditions`` refs (never duplicated inline).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# {{token}} placeholders in task-level text; token = a system_fields key or the
# root-anchored path of a context-role leaf (2026-07-08 spec §4).
PLACEHOLDER_RE = re.compile(r"\{\{([\w.]+)\}\}")
# A complete {{…}} pair whose innards did NOT parse as a token above — e.g.
# "{{ member_id }}" (inner whitespace) or "{{patient-name}}" (bad chars). These
# are operator typos that would otherwise reach the spoken prompt as literal
# braces, so validation flags them. A lone unclosed "{{" stays legal literal
# text (2026-07-06 spec §8).
_MALFORMED_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")


def malformed_placeholders(text: str) -> list[str]:
    """Brace pairs that look like placeholders but fail PLACEHOLDER_RE, in
    document order. Valid tokens are stripped first so only leftovers report."""
    return _MALFORMED_PLACEHOLDER_RE.findall(PLACEHOLDER_RE.sub("", text))


PATH_PREFIX = "sections."
MAX_PATH_LENGTH = 255
MAX_STT_KEY_TERMS = 100  # Deepgram keyterm-prompting limit

SectionRole = Literal["collect", "context", "ui_only"]
LeafRole = Literal["ask", "confirm", "context", "readonly", "input"]
LeafType = Literal["text", "enum", "date", "currency", "percent", "integer", "phone"]
ComparisonOp = Literal["eq", "ne", "in", "not_in"]
RANGE_TYPES: frozenset[str] = frozenset({"currency", "percent", "integer"})
COLLECTED_ROLES: frozenset[str] = frozenset({"ask", "confirm"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


class Comparison(_Model):
    field: str
    op: ComparisonOp
    value: str | list[str]

    @model_validator(mode="after")
    def _value_shape_matches_op(self) -> Comparison:
        if self.op in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError(f"op {self.op} requires a list value")
        if self.op in ("eq", "ne") and isinstance(self.value, list):
            raise ValueError(f"op {self.op} requires a scalar value")
        return self


class AllCondition(_Model):
    all: list[Condition]


class AnyCondition(_Model):
    any: list[Condition]


class NotCondition(_Model):
    not_: Condition = Field(alias="not")


class RefCondition(_Model):
    ref: str


Condition = Comparison | AllCondition | AnyCondition | NotCondition | RefCondition


def condition_field_paths(
    cond: Condition, shared: dict[str, Condition] | None, depth: int = 0
) -> Iterator[str]:
    """Every leaf path a condition references, shared refs expanded."""
    if depth > 10:
        return
    match cond:
        case Comparison(field=field):
            yield field
        case RefCondition(ref=ref):
            if shared and ref in shared:
                yield from condition_field_paths(shared[ref], shared, depth + 1)
        case AllCondition(all=subs) | AnyCondition(any=subs):
            for sub in subs:
                yield from condition_field_paths(sub, shared, depth + 1)
        case NotCondition(not_=sub):
            yield from condition_field_paths(sub, shared, depth + 1)


class ConfirmInTask(_Model):
    """Where and when a context-section confirm field is spoken (2026-07-08 spec §3.4)."""

    task_key: str = Field(description="The task during which this confirmation is spoken.")
    confirm_immediate: bool = Field(
        default=False,
        description=(
            "True: speak the confirmation immediately after the anchor question — "
            "the last collectable leaf in the named task referenced by this "
            "field's applicable_when gate chain — is answered and the gate holds. "
            "False: speak it at the end of the named task."
        ),
    )


# ---------------------------------------------------------------------------
# Field building blocks
# ---------------------------------------------------------------------------


class Range(_Model):
    min: int | float | None = None
    max: int | float | None = None

    @model_validator(mode="after")
    def _bounds_ordered(self) -> Range:
        if self.min is None and self.max is None:
            raise ValueError("range needs min and/or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("range min > max")
        return self


# Tokens legal in `date_format` (closed set — extend deliberately): month/day
# with or without a leading zero, 4-digit year, `/` `-` `.` separators. No 2-digit
# year (`YY`): unsafe on a DOB field — "55" is ambiguous between 1955 and 2055 —
# and never gets any better resolved by adding a pivot-year heuristic, so it's
# rejected outright rather than silently guessing a century.
DATE_FORMAT_RE = re.compile(r"^(?:MM?|DD?|YYYY)(?:[-/.](?:MM?|DD?|YYYY))*$")

_DATE_TOKEN_RE = re.compile(r"YYYY|MM?|DD?")
_DATE_TOKEN_PATTERNS: dict[str, str] = {
    "YYYY": r"(?P<year>\d{4})",
    "MM": r"(?P<month>\d{2})",
    "M": r"(?P<month>\d{1,2})",
    "DD": r"(?P<day>\d{2})",
    "D": r"(?P<day>\d{1,2})",
}


def parse_date_format(text: str, date_format: str) -> date | None:
    """Parse `text` against a leaf's display/entry `date_format` (e.g. "M/D/YYYY" —
    see `Validation.date_format`), for values a human typed in that format rather
    than ISO (the review UI prompts and validates against this same format; see
    `vera-frontend/src/lib/ibv/validation.ts`). Returns `None` on a shape or
    calendar mismatch — never raises; the caller decides whether that's an error."""
    pattern = ""
    pos = 0
    for m in _DATE_TOKEN_RE.finditer(date_format):
        pattern += re.escape(date_format[pos : m.start()]) + _DATE_TOKEN_PATTERNS[m.group()]
        pos = m.end()
    pattern += re.escape(date_format[pos:])
    try:
        match = re.fullmatch(pattern, text)
    except re.error:
        # A degenerate date_format with a repeated token (e.g. "M/M/YYYY") builds a
        # pattern with a duplicate named group, which re.compile rejects. `Validation`
        # rejects such formats at schema-authoring time, but this function can still be
        # called directly with an unchecked format — so treat it as "doesn't parse,"
        # same as any other mismatch, rather than raising on a malformed schema.
        return None
    if match is None:
        return None
    groups = match.groupdict()
    month, day, year = groups.get("month"), groups.get("day"), groups.get("year")
    if month is None or day is None or year is None:
        return None
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def format_date(value: date, date_format: str) -> str:
    """Render `value` in a leaf's declared display/entry `date_format` (e.g.
    "M/D/YYYY" — see `Validation.date_format`) — the inverse of
    `parse_date_format`. Used to normalize a date leaf's stored answer to one
    consistent shape regardless of which format the submitter used (ISO from a
    machine caller, or the declared format from a human editor)."""

    def render_token(match: re.Match[str]) -> str:
        token = match.group()
        if token == "YYYY":
            return f"{value.year:04d}"
        if token == "MM":
            return f"{value.month:02d}"
        if token == "M":
            return str(value.month)
        if token == "DD":
            return f"{value.day:02d}"
        return str(value.day)  # token == "D"

    return _DATE_TOKEN_RE.sub(render_token, date_format)


class Validation(_Model):
    pattern: str | None = None
    range: Range | None = None
    # Display/entry format for `type: date` values (e.g. "M/D/YYYY").
    date_format: str | None = None

    @model_validator(mode="after")
    def _pattern_compiles(self) -> Validation:
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern regex: {exc}") from exc
        if self.date_format is not None:
            tokens = _DATE_TOKEN_RE.findall(self.date_format)
            # Reduce each token to its kind via its first char (M/MM -> "M", D/DD -> "D",
            # YYYY -> "Y") and require exactly one of each, rejecting a missing or
            # repeated token.
            token_kinds = sorted(token[0] for token in tokens)
            if not DATE_FORMAT_RE.match(self.date_format) or token_kinds != ["D", "M", "Y"]:
                raise ValueError(
                    "date_format must contain exactly one M/MM, one D/DD and one YYYY "
                    "token, joined by -/. separators (no YY — a 2-digit year is "
                    "ambiguous on a date field; a missing or repeated token would "
                    "silently drop part of every stored date)"
                )
        return self


class Derive(_Model):
    when: Condition
    value: str


class RequiredWhen(_Model):
    when: Condition


class FieldPrompt(_Model):
    ask: str | None = None
    confirm: str | None = None
    hints: list[str] | None = None


class Codes(_Model):
    cpt: list[str] | None = None
    icd10: list[str] | None = None
    speak_cpt: bool | None = None


class Ui(_Model):
    widget: str | None = None
    layout: str | None = None


class Leaf(_Model):
    """A single collected/displayed value; canonical key order = declaration order."""

    type: LeafType
    title: str
    role: LeafRole
    required: bool | RequiredWhen = False
    values: list[str] | None = None
    special_values: list[str] | None = None
    default: str | None = None
    tags: list[str] | None = None
    validation: Validation | None = None
    inapplicable_value: str | None = None
    applicable_when: Condition | None = None
    derive: Derive | None = None
    confirm_in_task: ConfirmInTask | None = None
    codes: Codes | None = None
    prompt: FieldPrompt | None = None
    ui: Ui | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Leaf:
        if self.type == "enum" and not self.values:
            raise ValueError("enum field needs values")
        if self.type != "enum" and self.values is not None:
            raise ValueError("values only on enum fields (use special_values for text)")
        if self.validation is not None:
            if self.validation.pattern is not None and self.type == "enum":
                raise ValueError("validation.pattern not allowed on enum fields")
            if self.validation.range is not None and self.type not in RANGE_TYPES:
                raise ValueError(f"validation.range only on {sorted(RANGE_TYPES)}")
            if self.validation.date_format is not None and self.type != "date":
                raise ValueError("validation.date_format only on date fields")
        if self.prompt is not None:
            if self.prompt.confirm is not None and self.role != "confirm":
                raise ValueError("prompt.confirm on non-confirm role")
            if self.prompt.ask is not None and self.role != "ask":
                raise ValueError(f"prompt.ask on role {self.role}")
        if self.role == "ask" and not (self.prompt and self.prompt.ask):
            raise ValueError("ask field needs prompt.ask")
        if self.role == "confirm" and not (self.prompt and self.prompt.confirm):
            raise ValueError("confirm field needs prompt.confirm")
        if self.confirm_in_task is not None and self.role != "confirm":
            raise ValueError("confirm_in_task only valid on role=confirm")
        if self.tags is not None and not all(KEY_RE.match(t) for t in self.tags):
            raise ValueError("tags must be snake_case strings")
        return self


class Group(_Model):
    type: Literal["group"]
    title: str
    integrity: Literal["all", "any"] | None = None
    codes: Codes | None = None
    applicable_when: Condition | None = None
    prompt: FieldPrompt | None = None
    ui: Ui | None = None
    description: str | None = None
    fields: dict[str, FormField]

    @model_validator(mode="after")
    def _coherent(self) -> Group:
        if not self.fields:
            raise ValueError("group without fields")
        if self.prompt is not None and self.prompt.confirm is not None:
            raise ValueError("prompt.confirm not allowed on groups")
        return self


FormField = Annotated[Group | Leaf, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Section-level constructs
# ---------------------------------------------------------------------------


class SectionPrompt(_Model):
    intro: str


class AskGroup(_Model):
    """One combined spoken question over ≥2 distinct ask-role sibling leaves."""

    fields: list[str]
    ask: str


class Alternatives(_Model):
    """Either/or set: one member answered ⇒ others auto-record N/A, set complete."""

    members: list[str]
    ask: str | None = None


class Section(_Model):
    title: str
    role: SectionRole = "collect"
    description: str | None = None
    applicable_when: Condition | None = None
    codes: Codes | None = None
    prompt: SectionPrompt | None = None
    ask_groups: list[AskGroup] | None = None
    alternatives: list[Alternatives] | None = None
    ui: Ui | None = None
    fields: dict[str, FormField]


# ---------------------------------------------------------------------------
# Call-flow constructs
# ---------------------------------------------------------------------------


class Task(_Model):
    """One LiveKit AgentTask.

    ``intro``/``outro`` are spoken verbatim on task entry/exit (TTS-safe text);
    ``prompt`` is supplied directly as the agent's task instructions. All three
    may embed ``{{system_field_key}}`` placeholders, hydrated per patient form
    at task creation and validated against ``system_fields`` below. ``sections``
    may be empty for ritual tasks that collect nothing.
    """

    task_key: str
    title: str
    intro: str | None = None
    outro: str | None = None
    prompt: str | None = None
    sections: list[str]
    applicable_when: Condition | None = None


class FlowRule(_Model):
    rule_key: str
    when: Condition
    action: Literal["terminate_call"]
    skip_to_task: str | None = None
    note: str | None = None


class Contradiction(_Model):
    """Cross-field consistency rule: push back once and re-clarify `fields`."""

    rule_key: str
    when: Condition
    fields: list[str]
    reason: str
    clarify: str | None = None


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class PromotedFields(_Model):
    """patient_form column -> root-anchored leaf path.

    The attribute set mirrors PatientForm's promoted columns (searchable
    identifiers + worklist display fields); every schema must map all of them,
    so a new schema can neither forget nor typo a column (enforced at
    authoring/compile/load — extra="forbid", no defaults). Declaration order
    is the compiled-artifact key order. Consumed by
    vera_core.forms.intake.promote_columns and the dispute-resolve promotion
    in control_plane.api.v1.patient_forms.
    """

    patient_name: str
    patient_dob: str
    chart_number: str
    appointment_date: str
    appointment_type: str
    member_id: str
    insurance_provider: str
    insurance_provider_phone_number: str

    def items(self) -> list[tuple[str, str]]:
        """(column, leaf path) pairs, in declaration order."""
        return [(column, getattr(self, column)) for column in type(self).model_fields]


class FormSchemaDoc(_Model):
    dsl_version: Literal["2.1"]
    name: str
    insurance_type: str
    description: str | None = None
    system_fields: dict[str, str] | None = None
    # patient_form column name -> root-anchored leaf path. Required: every schema
    # must map every promotable column (PromotedFields). Each path must also be a
    # system_fields target (validated below) — that guarantees a promoted column can
    # never be *unexpectedly* empty at intake (system_fields targets are exactly what
    # required_intake_fields enforces at creation, intake.py), though a leaf with its
    # own `default` is still allowed to be absent from the payload (it counts as
    # filled either way).
    promoted_fields: PromotedFields
    # Session-wide STT vocabulary, fed verbatim to deepgram.STTv2(keyterms=...)
    # at voice-session build; applies to every task. Static domain terms only.
    stt_key_terms: list[str] | None = None
    shared_conditions: dict[str, Condition] | None = None
    sections: dict[str, Section]
    tasks: list[Task]
    flow_rules: list[FlowRule] | None = None
    contradictions: list[Contradiction] | None = None

    # -- documented walk helpers -------------------------------------------------

    def _iter_fields(self) -> Iterator[tuple[str, FormField]]:
        """Every (root-anchored path, field) pair — groups and leaves — in document order."""

        def walk(prefix: str, fields: dict[str, FormField]) -> Iterator[tuple[str, FormField]]:
            for key, field in fields.items():
                path = f"{prefix}.{key}"
                yield path, field
                if isinstance(field, Group):
                    yield from walk(path, field.fields)

        for section_key, section in self.sections.items():
            yield from walk(f"{PATH_PREFIX}{section_key}", section.fields)

    def leaf_items(self) -> list[tuple[str, Leaf]]:
        """Every (root-anchored path, leaf) pair, in document order."""
        return [(path, field) for path, field in self._iter_fields() if isinstance(field, Leaf)]

    def group_paths(self) -> set[str]:
        """Root-anchored paths of every group node."""
        return {path for path, field in self._iter_fields() if isinstance(field, Group)}

    def collection_paths(self, section_keys: list[str] | None = None) -> list[str]:
        """Voice-agent collection targets: role in (ask, confirm), optionally per section."""
        keys = set(section_keys) if section_keys is not None else None
        out: list[str] = []
        for path, leaf in self.leaf_items():
            if keys is not None and path.split(".")[1] not in keys:
                continue
            if leaf.role in COLLECTED_ROLES:
                out.append(path)
        return out

    # -- cross-document validation -------------------------------------------------

    @model_validator(mode="after")
    def _validate_document(self) -> FormSchemaDoc:
        errors: list[str] = []
        leaves = dict(self.leaf_items())
        groups = self.group_paths()
        shared = self.shared_conditions or {}
        task_keys = [t.task_key for t in self.tasks]

        def check_condition(where: str, cond: Condition, depth: int = 0) -> None:
            if depth > 10:
                errors.append(f"{where}: condition nesting too deep")
                return
            match cond:
                case RefCondition(ref=ref):
                    if ref not in shared:
                        errors.append(f"{where}: unknown shared condition ref {ref!r}")
                case Comparison(field=field):
                    if field not in leaves:
                        errors.append(f"{where}: path {field!r} does not resolve to a leaf field")
                case AllCondition(all=subs) | AnyCondition(any=subs):
                    for i, sub in enumerate(subs):
                        check_condition(f"{where}[{i}]", sub, depth + 1)
                case NotCondition(not_=sub):
                    check_condition(f"{where}.not", sub, depth + 1)

        def check_key(where: str, key: str) -> None:
            if not KEY_RE.match(key):
                errors.append(f"{where}: bad key {key!r}")

        # fields, roles, gating
        immediate_confirms: list[tuple[str, ConfirmInTask, tuple[Condition, ...]]] = []

        def walk_fields(
            prefix: str,
            fields: dict[str, FormField],
            section: Section,
            chain: tuple[Condition, ...],
        ) -> None:
            for key, field in fields.items():
                path = f"{prefix}.{key}"
                check_key(path, key)
                if len(path) > MAX_PATH_LENGTH:
                    errors.append(f"{path}: exceeds {MAX_PATH_LENGTH} chars")
                field_chain = (
                    (*chain, field.applicable_when) if field.applicable_when is not None else chain
                )
                if field.applicable_when is not None:
                    check_condition(f"{path}.applicable_when", field.applicable_when)
                if isinstance(field, Group):
                    walk_fields(path, field.fields, section, field_chain)
                    continue
                if (
                    field.role in COLLECTED_ROLES
                    and section.role != "collect"
                    and field.confirm_in_task is None
                ):
                    errors.append(
                        f"{path}: role {field.role} outside a collect section "
                        "requires confirm_in_task"
                    )
                if (
                    field.confirm_in_task is not None
                    and field.confirm_in_task.task_key not in task_keys
                ):
                    errors.append(f"{path}: confirm_in_task references unknown task")
                if isinstance(field.required, RequiredWhen):
                    check_condition(f"{path}.required.when", field.required.when)
                if field.derive is not None:
                    check_condition(f"{path}.derive.when", field.derive.when)
                if field.inapplicable_value is not None and not field_chain:
                    errors.append(
                        f"{path}: inapplicable_value without applicable_when on self or ancestor"
                    )
                if field.confirm_in_task is not None and field.confirm_in_task.confirm_immediate:
                    immediate_confirms.append((path, field.confirm_in_task, field_chain))

        for section_key, section in self.sections.items():
            check_key(f"section {section_key}", section_key)
            if section.applicable_when is not None:
                check_condition(f"section {section_key}.applicable_when", section.applicable_when)
            walk_fields(
                f"{PATH_PREFIX}{section_key}",
                section.fields,
                section,
                (section.applicable_when,) if section.applicable_when is not None else (),
            )

        # shared conditions
        for name, cond in shared.items():
            check_key(f"shared_conditions {name}", name)
            check_condition(f"shared_conditions.{name}", cond)

        # tasks: every collect section in exactly one task
        assigned: list[str] = []
        context_paths = {p for p, leaf in leaves.items() if leaf.role == "context"}
        for task in self.tasks:
            check_key(f"task {task.task_key}", task.task_key)
            if task.applicable_when is not None:
                check_condition(f"task {task.task_key}.applicable_when", task.applicable_when)
            for skey in task.sections:
                task_section = self.sections.get(skey)
                if task_section is None:
                    errors.append(f"task {task.task_key}: unknown section {skey!r}")
                    continue
                if skey in assigned:
                    errors.append(f"section {skey!r} assigned to more than one task")
                assigned.append(skey)
                if task_section.role != "collect":
                    errors.append(
                        f"task {task.task_key}: section {skey!r} has role "
                        f"{task_section.role!r}, only collect sections belong to tasks"
                    )
            for attr in ("intro", "outro", "prompt"):
                text: str | None = getattr(task, attr)
                for token in PLACEHOLDER_RE.findall(text or ""):
                    if token not in (self.system_fields or {}) and token not in context_paths:
                        errors.append(
                            f"task {task.task_key}.{attr}: unknown placeholder "
                            f"{{{{{token}}}}} (not a system_fields key or context-leaf path)"
                        )
                for snippet in malformed_placeholders(text or ""):
                    errors.append(
                        f"task {task.task_key}.{attr}: malformed placeholder {snippet!r} "
                        "(use {{token}} — word characters and dots only, no spaces)"
                    )
        if len(set(task_keys)) != len(task_keys):
            errors.append("duplicate task_key")
        for skey, section in self.sections.items():
            if section.role == "collect" and skey not in assigned:
                errors.append(f"collect section {skey!r} not assigned to any task")

        # confirm_immediate needs a determinable anchor inside its task
        task_sections = {t.task_key: set(t.sections) for t in self.tasks}
        for path, cit, chain in immediate_confirms:
            in_task = task_sections.get(cit.task_key, set())
            refs = {ref for cond in chain for ref in condition_field_paths(cond, shared)}
            if not any(
                ref in leaves
                and leaves[ref].role in COLLECTED_ROLES
                and ref.split(".")[1] in in_task
                for ref in refs
            ):
                errors.append(
                    f"{path}: confirm_immediate=true needs an anchor — the gate chain "
                    f"must reference a collectable leaf inside task {cit.task_key!r}"
                )

        # ask_groups / alternatives
        for skey, section in self.sections.items():
            prefix = f"{PATH_PREFIX}{skey}."
            seen_ag: set[str] = set()
            for i, ag in enumerate(section.ask_groups or []):
                where = f"section {skey}.ask_groups[{i}]"
                if len(ag.fields) < 2:
                    errors.append(f"{where}: needs at least 2 member fields")
                for member in ag.fields:
                    if member not in leaves:
                        errors.append(f"{where}: member {member!r} is not a leaf field")
                    elif not member.startswith(prefix):
                        errors.append(f"{where}: member {member!r} is outside this section")
                    elif leaves[member].role != "ask":
                        errors.append(f"{where}: member {member!r} must have role ask")
                    if member in seen_ag:
                        errors.append(f"{where}: member {member!r} in more than one ask group")
                    seen_ag.add(member)
            seen_alt: set[str] = set()
            for i, alt in enumerate(section.alternatives or []):
                where = f"section {skey}.alternatives[{i}]"
                if len(alt.members) < 2:
                    errors.append(f"{where}: needs at least 2 members")
                for member in alt.members:
                    if member not in leaves and member not in groups:
                        errors.append(f"{where}: member {member!r} is not a field or group")
                    elif not member.startswith(prefix):
                        errors.append(f"{where}: member {member!r} is outside this section")
                    elif member in leaves and leaves[member].role != "ask":
                        errors.append(f"{where}: leaf member {member!r} must have role ask")
                    if member in seen_alt:
                        errors.append(f"{where}: member {member!r} in more than one alternatives")
                    seen_alt.add(member)

        # system fields
        for handle, path in (self.system_fields or {}).items():
            check_key(f"system_fields {handle}", handle)
            if path not in leaves:
                errors.append(f"system_fields.{handle}: {path!r} does not resolve to a leaf")

        # promoted fields — patient_form columns re-derived from the current answer at
        # dispute-resolve time too (not just intake). Column names are enforced by the
        # PromotedFields model itself; each path must be a system_fields target so a
        # promoted column is never legitimately empty.
        system_field_paths = set((self.system_fields or {}).values())
        for column, path in self.promoted_fields.items():
            if path not in leaves:
                errors.append(f"promoted_fields.{column}: {path!r} does not resolve to a leaf")
            elif path not in system_field_paths:
                errors.append(
                    f"promoted_fields.{column}: {path!r} is not a system_fields target "
                    "(promoted fields must be guaranteed present at intake)"
                )

        # stt key terms: bounded, unique, static vocabulary
        terms = self.stt_key_terms or []
        if len(terms) > MAX_STT_KEY_TERMS:
            errors.append(f"stt_key_terms: {len(terms)} terms exceeds limit of {MAX_STT_KEY_TERMS}")
        seen_terms: set[str] = set()
        for i, term in enumerate(terms):
            where = f"stt_key_terms[{i}]"
            if not term or term != term.strip():
                errors.append(f"{where}: empty or untrimmed term {term!r}")
                continue
            if "{{" in term:
                errors.append(f"{where}: placeholders are not allowed in key terms")
            lowered = term.lower()
            if lowered in seen_terms:
                errors.append(f"{where}: duplicate term {term!r}")
            seen_terms.add(lowered)

        # flow rules
        for rule in self.flow_rules or []:
            check_key(f"flow_rule {rule.rule_key}", rule.rule_key)
            check_condition(f"flow_rule {rule.rule_key}.when", rule.when)
            if rule.skip_to_task is not None and rule.skip_to_task not in task_keys:
                errors.append(f"flow_rule {rule.rule_key}: unknown skip_to_task")

        # contradictions
        rule_keys: set[str] = set()
        for contradiction in self.contradictions or []:
            rk = contradiction.rule_key
            check_key(f"contradiction {rk}", rk)
            if rk in rule_keys:
                errors.append(f"duplicate contradiction rule_key {rk!r}")
            rule_keys.add(rk)
            check_condition(f"contradiction {rk}.when", contradiction.when)
            if not contradiction.fields:
                errors.append(f"contradiction {rk}: needs at least one field to re-clarify")
            for member in contradiction.fields:
                if member not in leaves:
                    errors.append(f"contradiction {rk}: {member!r} is not a leaf field")
                elif leaves[member].role not in COLLECTED_ROLES:
                    errors.append(
                        f"contradiction {rk}: {member!r} has role {leaves[member].role!r} — "
                        "only ask/confirm fields can be re-clarified"
                    )

        if errors:
            raise ValueError(
                "invalid form schema document:\n" + "\n".join(f"- {e}" for e in errors)
            )
        return self


# ---------------------------------------------------------------------------
# Compiler / loader
# ---------------------------------------------------------------------------


def compile_document(doc: FormSchemaDoc) -> str:
    """Serialize to the canonical compiled-artifact text (deterministic key order)."""
    data = doc.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load_document(text: str) -> FormSchemaDoc:
    """Parse + fully validate a compiled artifact (round-trips with compile_document)."""
    return FormSchemaDoc.model_validate_json(text)


# Recursive models (Condition inside itself, FormField inside Group) need their
# deferred annotations resolved once the whole module namespace exists.
for _model in (AllCondition, AnyCondition, NotCondition, Group, Section, FormSchemaDoc):
    _model.model_rebuild()
