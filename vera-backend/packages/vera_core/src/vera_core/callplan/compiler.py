"""Schema → CallPlan compiler.

Pure JSON transform (no DB, no I/O): a published `SchemaVersion.schema_json`
plus the caller's DB prefill becomes the per-call `CallPlan` the agent worker
runs. Fail-closed: any structural problem raises `CompileError` at POST /calls
(where it is a clean 5xx/409), never mid-call.

Prefilled DB-known values flow into the plan as raw values (confirm values +
placeholder substitution). PHI tokenization / sealing was removed (dev
simplification), so the plan holds plaintext prefilled PHI — synthetic-data-only
until a protection mechanism is reintroduced (see adr/devops-todo.md #8).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from vera_core.callplan.model import (
    CallPlan,
    FieldMetadata,
    FieldPolicy,
    FieldRule,
    PlanField,
    PlanFieldGroup,
    PlanSection,
    RuleCondition,
    RuleEffect,
)
from vera_core.schemas import PersonaTweak


class CompileError(ValueError):
    """The schema document cannot be compiled into a runnable plan."""


# ---------------------------------------------------------------------------
# Constants shared with the worker (prompt.py re-exports for its fallback path)

DEFAULT_GREETING = (
    "Hi, I'm calling on behalf of a patient to verify their infertility treatment "
    "coverage under this plan. Do you have a few minutes to go through the benefits?"
)

CARTESIA_MARKUP_GUIDE = """SPOKEN MARKUP (Cartesia TTS only)
Cartesia Sonic 3.5 sounds natural from plain prose, so keep writing plain sentences — tone comes from your word choice, not markup. Tone and pacing are already set on the voice itself. Only two inline tags are supported, and they are the sole exception to the plain-sentences rule above:

- <spell>...</spell> reads the contents one character at a time, which is the most reliable way to voice a code. Wrap every CPT code in it using the bare digit string, e.g. <spell>58340</spell>, instead of writing the digits out as words. For an ICD-10 code, spell each side of the decimal and say the point in prose, e.g. <spell>Z31</spell> point <spell>89</spell>.
- <break time="200ms"/> inserts a short pause between two distinct thoughts. Use it rarely — at most once per response, and never chain two breaks.

Do not use any other tags (no emotion tags — they are not a Sonic 3.5 feature and will be read aloud). Never speak a tag name out loud. Never wrap a tool call in a tag."""

_NEUTRAL_PHI = "the value on file"

# Schema `rules[].effect` prose → normalized RuleEffect.
_RULE_EFFECTS: dict[str, RuleEffect] = {
    "make this required": RuleEffect.MAKE_REQUIRED,
    "terminate_call_when": RuleEffect.TERMINATE_CALL_WHEN,
    "ask this question": RuleEffect.ASK_QUESTION,
    "auto-fill a value": RuleEffect.AUTO_FILL,
}

# PHI placeholders appearing in fragment prose, mapped to the leaf field keys
# whose prefill supplies the value. (`{member_id}` is authored against the
# `policy_number` field, etc.)
_PHI_PLACEHOLDER_FIELDS: dict[str, tuple[str, ...]] = {
    "patient_name": ("patient_name",),
    "date_of_birth": ("patient_dob", "date_of_birth"),
    "member_id": ("policy_number", "member_id"),
    "group_number": ("group_number",),
    "spouse_name": ("spouse_partner_name", "spouse_name"),
    "spouse_dob": ("spouse_partner_dob", "spouse_dob"),
    "appointment_date": ("appointment_date",),
}

# Structural placeholders that resolve to whole fragments from the index.
_FRAGMENT_PLACEHOLDERS: dict[str, str] = {
    "persona": "AGENT_PERSONA",
    "complete_phase_tool": "COMPLETE_PHASE_TOOL",
    "turn_taking_rules": "TURN_TAKING_RULES",
    "background_noise_rules": "BACKGROUND_NOISE_RULES",
    "anti_repetition_rules": "ANTI_REPETITION_RULES",
    "role_enforcement_rules": "ROLE_ENFORCEMENT_RULES",
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_SECTIONS_REF = "<SECTIONS>"

# The shared, phase-agnostic rule fragments composing the M1 flat prompt.
# Phase-specific fragments (PHASE_N_START etc.) are per-section material (M2).
_FLAT_PROMPT_FRAGMENTS = (
    "AGENT_PERSONA",
    "PRONUNCIATION_GUIDE",
    "TURN_TAKING_RULES",
    "TURN_TAKING_FUNCTION_RULE",
    "BACKGROUND_NOISE_RULES",
    "ANTI_REPETITION_RULES",
    "ECHO_AWARENESS_RULES",
    "ROLE_ENFORCEMENT_RULES",
    "INCOMPLETE_RESPONSE_RULE",
    "VALUE_RECORDING_RULES",
    "UNAVAILABLE_INFO_RULES",
    "NUMBER_SPEAKING_RULES",
    "CPT_PHONETIC_RULES",
    "TEEN_TY_DISAMBIGUATION",
    "IDENTITY_GUARDRAILS",
    "FIXED_ANSWERS_GUARDRAILS",
    "FORBIDDEN_ACTIONS",
)


@dataclass(frozen=True)
class _Fragment:
    name: str
    text: str
    order: int


def _fragment_index(schema_json: Mapping[str, Any]) -> dict[str, _Fragment]:
    """All prose fragments — `global_policies` plus every section's
    `section_policies` — keyed by the UPPER-normalized `source` suffix."""
    index: dict[str, _Fragment] = {}

    def _add(policy: Mapping[str, Any]) -> None:
        source = policy.get("source")
        text = policy.get("exact_text")
        if not isinstance(source, str) or not isinstance(text, str):
            raise CompileError(f"policy fragment missing source/exact_text: {policy.get('title')}")
        name = source.split(":")[-1].strip()
        order = policy.get("order")
        index[name.upper()] = _Fragment(
            name=name, text=text, order=order if isinstance(order, int) else 0
        )

    for policy in schema_json.get("global_policies", []):
        _add(policy)
    for section in schema_json.get("sections", []):
        for policy in section.get("section_policies", []):
            _add(policy)
    return index


def _lookup(index: Mapping[str, _Fragment], ref: str) -> _Fragment:
    fragment = index.get(ref.upper())
    if fragment is None:
        raise CompileError(f"phase recipe references unknown fragment {ref!r}")
    return fragment


# ---------------------------------------------------------------------------
# Field flattening


def _parse_rules(raw_rules: Any, field_path: str) -> list[FieldRule]:
    rules: list[FieldRule] = []
    for raw in raw_rules or []:
        effect = _RULE_EFFECTS.get(raw.get("effect", ""))
        if effect is None:
            raise CompileError(f"unknown rule effect {raw.get('effect')!r} on {field_path}")
        rules.append(
            FieldRule(
                effect=effect,
                match=raw.get("match", "all of these"),
                conditions=[
                    RuleCondition(
                        field=c.get("field", ""),
                        comparison=c.get("comparison", "is"),
                        value=str(c.get("value", "")),
                    )
                    for c in raw.get("conditions", [])
                ],
                summary=raw.get("summary"),
            )
        )
    return rules


def _prompt_dict(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """The field's `prompt` object, or an empty mapping when absent/malformed."""
    prompt = raw.get("prompt")
    return prompt if isinstance(prompt, dict) else {}


def _parse_metadata(raw: Mapping[str, Any]) -> FieldMetadata | None:
    """Billing-code metadata (`cpt_codes`, `icd10`), or None when absent."""
    metadata_raw = raw.get("metadata")
    if not isinstance(metadata_raw, dict):
        return None
    return FieldMetadata(
        cpt_codes=[str(c) for c in metadata_raw.get("cpt_codes", [])],
        icd10=metadata_raw.get("icd10"),
    )


def _parse_policies(raw: Mapping[str, Any]) -> list[FieldPolicy]:
    """The field's behavioral checkpoints (`policies[]`)."""
    return [
        FieldPolicy(
            title=p.get("title", ""),
            verbatim=bool(p.get("verbatim", False)),
            exact_text=p.get("exact_text", ""),
        )
        for p in raw.get("policies", [])
    ]


def _flatten_field(
    section_key: str,
    field_key: str,
    raw: Mapping[str, Any],
    constraint_library: Mapping[str, Any],
    section_required: frozenset[str],
    *,
    parent_path: str | None = None,
) -> tuple[list[PlanField], list[PlanFieldGroup]]:
    """One schema field → leaf PlanFields plus the PlanFieldGroups for any
    object-typed parents (their ask-context lives on the group, once)."""
    path = f"{parent_path or section_key}.{field_key}"
    children = raw.get("properties")
    if isinstance(children, dict) and children:
        group = PlanFieldGroup(
            group_key=path,
            title=raw.get("title", field_key),
            description=raw.get("description"),
            verbatim_prompt=raw.get("verbatim_prompt"),
            ask_prompt=_prompt_dict(raw).get("ask"),
            metadata=_parse_metadata(raw),
            rules=_parse_rules(raw.get("rules"), path),
            policies=_parse_policies(raw),
            group_integrity=raw.get("group_integrity"),
        )
        nested_fields: list[PlanField] = []
        nested_groups: list[PlanFieldGroup] = [group]
        for child_key, child_raw in children.items():
            child_fields, child_groups = _flatten_field(
                section_key,
                child_key,
                child_raw,
                constraint_library,
                frozenset(raw.get("required", [])),
                parent_path=path,
            )
            nested_fields.extend(
                f.model_copy(update={"group_key": path}) if f.group_key is None else f
                for f in child_fields
            )
            nested_groups.extend(child_groups)
        return nested_fields, nested_groups

    enum = raw.get("enum")
    constraint_ref = raw.get("constraint_ref")
    if enum is None and constraint_ref is not None:
        constraint = constraint_library.get(constraint_ref)
        if constraint is None:
            raise CompileError(f"unknown constraint_ref {constraint_ref!r} on {path}")
        enum = constraint.get("values")

    prompt = _prompt_dict(raw)
    field = PlanField(
        field_path=path,
        title=raw.get("title", field_key),
        description=raw.get("description"),
        type=raw.get("type", "string"),
        prompt_role=raw.get("prompt_role"),
        required=raw.get("required_state") == "required" or field_key in section_required,
        enum=[str(v) for v in enum] if enum is not None else None,
        constraint_ref=constraint_ref,
        verbatim_prompt=raw.get("verbatim_prompt"),
        ask_prompt=prompt.get("ask"),
        ask_category=prompt.get("category"),
        metadata=_parse_metadata(raw),
        rules=_parse_rules(raw.get("rules"), path),
        policies=_parse_policies(raw),
        group_integrity=raw.get("group_integrity"),
    )
    return [field], []


# ---------------------------------------------------------------------------
# Prefill overlay + compile-time rule resolution


def _leaf(field_path: str) -> str:
    return field_path.rsplit(".", 1)[-1]


def _rule_holds(rule: FieldRule, answers_by_leaf: Mapping[str, str]) -> bool | None:
    """Evaluate a rule against prefilled answers. None = not decidable yet
    (some condition references a field with no prefill)."""
    verdicts: list[bool] = []
    for cond in rule.conditions:
        known = answers_by_leaf.get(cond.field)
        if known is None:
            return None
        # The only comparison the DSL authors today; anything richer lands with
        # the runtime rules engine (M4) and gets a shared evaluator then.
        verdicts.append(known.strip().casefold() == cond.value.strip().casefold())
    if rule.match == "any of these":
        return any(verdicts)
    return all(verdicts)


def _apply_prefill(
    field: PlanField,
    raw: Mapping[str, Any],
    prefill: Mapping[str, str],
    answers_by_leaf: Mapping[str, str],
) -> PlanField:
    """Overlay DB knowledge on one field: prefilled → confirm-mode with the raw
    value; decidable rules are resolved now and dropped from the plan."""
    updates: dict[str, Any] = {}

    value = prefill.get(field.field_path)
    if value is not None:
        updates["mode"] = "confirm"
        updates["confirm_value"] = value
    elif raw.get("confirm_only"):
        # confirm_only without a DB value cannot be confirmed — ask instead
        # (fail-safe: never read back a value we do not hold).
        updates["mode"] = "ask"

    pending: list[FieldRule] = []
    required = field.required
    for rule in field.rules:
        verdict = _rule_holds(rule, answers_by_leaf)
        if verdict is None or rule.effect is not RuleEffect.MAKE_REQUIRED:
            # Undecidable now, or an effect only the runtime can act on.
            pending.append(rule)
        elif verdict:
            required = True
    if pending != field.rules:
        updates["rules"] = pending
    if required != field.required:
        updates["required"] = required

    return field.model_copy(update=updates) if updates else field


# ---------------------------------------------------------------------------
# Placeholder substitution


def _substitute(
    text: str,
    *,
    index: Mapping[str, _Fragment],
    values: Mapping[str, str],
    computed: Mapping[str, str],
    depth: int = 0,
) -> str:
    """Replace known `{placeholder}`s only; unknown braces (JSON examples,
    template internals) are left untouched. Fragment placeholders resolve
    recursively with a small depth bound. `values` merges the PHI-field
    placeholders (patient_name, member_id, ...) and the prose context
    placeholders (clinic_name, npi, ...) — disjoint namespaces."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        if name in _PHI_PLACEHOLDER_FIELDS:
            return _NEUTRAL_PHI  # PHI placeholder with no prefilled value
        if name in computed:
            return computed[name]
        if name in _FRAGMENT_PLACEHOLDERS and depth < 3:
            fragment = index.get(_FRAGMENT_PLACEHOLDERS[name].upper())
            if fragment is not None:
                return _substitute(
                    fragment.text,
                    index=index,
                    values=values,
                    computed=computed,
                    depth=depth + 1,
                )
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def _phi_value_map(answers_by_leaf: Mapping[str, str]) -> dict[str, str]:
    """placeholder name → raw prefilled value, resolved via the leaf-field aliases."""
    values: dict[str, str] = {}
    for placeholder, leaves in _PHI_PLACEHOLDER_FIELDS.items():
        for leaf in leaves:
            value = answers_by_leaf.get(leaf)
            if value is not None:
                values[placeholder] = value
                break
    return values


# ---------------------------------------------------------------------------
# Rendering


def _metadata_lines(metadata: FieldMetadata | None) -> list[str]:
    """The rendered CPT / ICD-10 lines for a field or group, or [] when absent."""
    if metadata is None:
        return []
    lines: list[str] = []
    if metadata.cpt_codes:
        codes = ", ".join(f"<spell>{c}</spell>" for c in metadata.cpt_codes)
        lines.append(f"  CPT codes: {codes}")
    if metadata.icd10:
        lines.append(f"  ICD-10 code: {metadata.icd10}")
    return lines


def _render_field(field: PlanField) -> str:
    lines = [f"- {field.title}" + (" (REQUIRED)" if field.required else "")]
    if field.mode == "confirm":
        lines.append(
            f"  CONFIRM ONLY: the value on file is {field.confirm_value or _NEUTRAL_PHI}. "
            "Read it back and confirm it; do not ask for it afresh."
        )
    ask = field.verbatim_prompt or field.ask_prompt
    if ask:
        lines.append(f"  Ask: {ask}")
    elif field.description:
        lines.append(f"  {field.description}")
    if field.enum:
        lines.append(f"  Allowed answers: {' | '.join(field.enum)}")
    lines.extend(_metadata_lines(field.metadata))
    if field.group_integrity == "all_or_nothing":
        lines.append("  This field's group is all-or-nothing: collect every part or none.")
    for rule in field.rules:
        lines.append(f"  Rule: {rule.summary or _describe_rule(rule)}")
    for policy in field.policies:
        lines.append(policy.exact_text)
    return "\n".join(lines)


def _describe_rule(rule: FieldRule) -> str:
    conds = " and ".join(f"{c.field} {c.comparison} {c.value!r}" for c in rule.conditions)
    return f"{rule.effect.value.replace('_', ' ')} when {conds}"


def _render_group(group: PlanFieldGroup, members: list[PlanField]) -> str:
    lines = [f"- {group.title}"]
    if group.group_integrity == "all_or_nothing":
        lines[0] += " (collect every data point below or none)"
    ask = group.verbatim_prompt or group.ask_prompt
    if ask:
        lines.append(f"  Ask: {ask}")
    elif group.description:
        lines.append(f"  {group.description}")
    lines.extend(_metadata_lines(group.metadata))
    for rule in group.rules:
        lines.append(f"  Rule: {rule.summary or _describe_rule(rule)}")
    for policy in group.policies:
        lines.append(policy.exact_text)
    lines.append("  Data points to record:")
    for field in members:
        detail = f" — allowed: {' | '.join(field.enum)}" if field.enum else ""
        lines.append(f"    - {field.title}{'  (REQUIRED)' if field.required else ''}{detail}")
    return "\n".join(lines)


def _render_section_block(
    section_key: str,
    title: str,
    fields: list[PlanField],
    groups: list[PlanFieldGroup] | None = None,
) -> str:
    by_group: dict[str, list[PlanField]] = {}
    loose: list[PlanField] = []
    for field in fields:
        if field.group_key is not None:
            by_group.setdefault(field.group_key, []).append(field)
        else:
            loose.append(field)
    blocks = [_render_field(f) for f in loose]
    for group in groups or []:
        members = by_group.get(group.group_key)
        if members:
            blocks.append(_render_group(group, members))
    rendered = "\n".join(blocks)
    return f'<section name="{section_key}">\n{title}\n{rendered}\n</section>'


def _questions_list(fields: list[PlanField]) -> str:
    return "\n".join(f"- {f.title}" for f in fields if f.mode != "skip")


# ---------------------------------------------------------------------------
# Entry point


def compile_call_plan(
    schema_json: Mapping[str, Any],
    prefill: Mapping[str, str],
    tweak: PersonaTweak | None,
    *,
    room_name: str,
    tenant_id: str,
    call_id: str,
    schema_version_id: str,
    prompt_version_id: str | None = None,
    context_values: Mapping[str, str] | None = None,
) -> CallPlan:
    """Compile a published schema into the per-call plan.

    `prefill` is keyed by field_path and holds the raw DB-known values; they
    flow into the plan as-is (confirm values + placeholder substitution).
    `context_values` supplies the non-PHI prose placeholders (clinic_name,
    verified_by, npi, ...); anything missing is left as authored.
    """
    index = _fragment_index(schema_json)
    constraint_library = schema_json.get("constraint_library", {})
    phase_recipes = schema_json.get("phase_order")
    if not isinstance(phase_recipes, dict) or not phase_recipes:
        raise CompileError("schema has no phase_order recipes")
    phase_keys = list(phase_recipes)

    answers_by_leaf = {_leaf(field_path): value for field_path, value in prefill.items()}
    # One placeholder→value map: prose context (clinic_name, npi, ...) plus the
    # prefilled PHI-field placeholders (patient_name, member_id, ...). Disjoint
    # namespaces, so the merge is unambiguous.
    values = {**(context_values or {}), **_phi_value_map(answers_by_leaf)}

    sections: list[PlanSection] = []
    for raw_section in schema_json.get("sections", []):
        section_key = raw_section.get("section_key")
        if not isinstance(section_key, str):
            raise CompileError("section missing section_key")
        is_context = raw_section.get("section_role") == "context"
        phase_key = raw_section.get("phase_key")
        if not is_context and phase_key not in phase_recipes:
            raise CompileError(
                f"section {section_key!r} has no valid phase_key (got {phase_key!r})"
            )

        section_required = frozenset(raw_section.get("required", []))
        fields: list[PlanField] = []
        groups: list[PlanFieldGroup] = []
        raw_by_path: dict[str, Mapping[str, Any]] = {}
        for field_key, raw_field in (raw_section.get("properties") or {}).items():
            flat_fields, flat_groups = _flatten_field(
                section_key, field_key, raw_field, constraint_library, section_required
            )
            groups.extend(flat_groups)
            for flat in flat_fields:
                # The confirm_only flag lives on the AUTHORED field; nested
                # leaves inherit their immediate raw definition.
                raw_by_path[flat.field_path] = (
                    raw_field if flat.field_path == f"{section_key}.{field_key}" else {}
                )
                fields.append(flat)
        fields = [
            _apply_prefill(f, raw_by_path.get(f.field_path, {}), prefill, answers_by_leaf)
            for f in fields
        ]

        mode: Literal["collect", "confirm", "context", "skip"]
        if is_context:
            mode = "context"
        elif fields and all(f.mode == "confirm" for f in fields):
            mode = "confirm"
        elif fields and all(f.mode == "skip" for f in fields):
            mode = "skip"
        else:
            mode = "collect"

        instructions = ""
        if not is_context and isinstance(phase_key, str):
            computed = {
                "phase_number": str(phase_keys.index(phase_key) + 1),
                "total_phases": str(len(phase_keys)),
                "questions_list": _questions_list(fields),
            }
            parts: list[str] = []
            for ref in phase_recipes[phase_key]:
                if ref == _SECTIONS_REF:
                    parts.append(
                        _render_section_block(
                            section_key, raw_section.get("title", section_key), fields, groups
                        )
                    )
                else:
                    parts.append(_lookup(index, ref).text)
            body = "\n\n".join(parts)
            instructions = _substitute(
                body,
                index=index,
                values=values,
                computed=computed,
            )
            if tweak is not None and tweak.extra_instructions:
                instructions = f"{instructions}\n\n{tweak.extra_instructions}"
            instructions = f"{instructions}\n\n{CARTESIA_MARKUP_GUIDE}"

        sections.append(
            PlanSection(
                section_key=section_key,
                title=raw_section.get("title", section_key),
                description=raw_section.get("description"),
                phase_key=phase_key if isinstance(phase_key, str) else "",
                mode=mode,
                instructions=instructions,
                fields=fields,
                groups=groups,
            )
        )

    flat_instructions = _flat_instructions(sections, index, values, tweak)
    greeting = tweak.greeting if tweak is not None and tweak.greeting else DEFAULT_GREETING
    return CallPlan(
        room_name=room_name,
        tenant_id=tenant_id,
        call_id=call_id,
        schema_version_id=schema_version_id,
        prompt_version_id=prompt_version_id,
        greeting=greeting,
        flat_instructions=flat_instructions,
        sections=sections,
    )


def _flat_instructions(
    sections: list[PlanSection],
    index: Mapping[str, _Fragment],
    values: Mapping[str, str],
    tweak: PersonaTweak | None,
) -> str:
    """The M1 single-agent prompt: persona + the shared phase-agnostic rule
    fragments + every non-context section's field block in schema order.
    (M2 replaces this with per-section agents running `PlanSection.instructions`.)"""
    parts: list[str] = []
    for name in _FLAT_PROMPT_FRAGMENTS:
        fragment = index.get(name.upper())
        if fragment is not None:
            parts.append(fragment.text)

    parts.append(
        "SECTIONS TO VERIFY (in order)\n"
        "Work through every section below in order. A field marked CONFIRM ONLY is "
        "already on file: read the value back for confirmation instead of asking. "
        "Skip nothing else. When every section is complete, say a brief polite "
        "closing line and then call the end_call tool to hang up."
    )
    for section in sections:
        if section.mode == "context":
            confirmables = [f for f in section.fields if f.mode == "confirm"]
            if confirmables:
                parts.append(
                    _render_section_block(
                        section.section_key, section.title, confirmables, section.groups
                    )
                )
            continue
        parts.append(
            _render_section_block(
                section.section_key, section.title, section.fields, section.groups
            )
        )

    body = _substitute(
        "\n\n".join(parts),
        index=index,
        values=values,
        computed={},
    )
    if tweak is not None and tweak.extra_instructions:
        body = f"{body}\n\n{tweak.extra_instructions}"
    return f"{body}\n\n{CARTESIA_MARKUP_GUIDE}"
