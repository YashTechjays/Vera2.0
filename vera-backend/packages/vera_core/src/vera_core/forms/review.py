"""Pure, DB-free helpers for the IBV review + dispute-resolution endpoints.

Kept free of SQLAlchemy/FastAPI so they unit-test without a database: the endpoint
queries the ORM, maps rows into the small `AnswerRow` value objects here, and this
module assembles the field views, dispute flags, completion %, and the
adjudication-action choice.

A field is "disputed" when its current `field_answer` came from the AI call
(`source='ai_call'`) and its value diverges from the **baseline** — the most recent
`intake`/`human` answer for that path (`IS DISTINCT FROM` semantics: an absent baseline
counts as `NULL`, so a divergent AI value is disputed even with no prior). The signal is
derived purely from `field_answer` history — `field_evaluation` plays no part, and
`dispute_action` is a pure audit record that does not gate the dispute. PHI lives in the
values — callers never log them.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from vera_core.forms.conditions import (
    AlternativeIndex,
    alternative_index,
    alternative_pairs,
    is_applicable,
    is_required,
    is_satisfied,
    is_v2,
    leaf_gates,
)
from vera_core.forms.dsl import COLLECTED_ROLES, FormSchemaDoc
from vera_core.models.enums import AnswerSource, DisputeActionType

# A judge verdict below this confidence (or unsupported) routes the field to review,
# and an AI answer below it is not "satisfied" for the retry decision. The single
# default for the whole post-call pipeline; `settings.post_call_review_floor` overrides
# it at consumer wiring time.
REVIEW_CONFIDENCE_FLOOR = 70


@dataclass(frozen=True)
class AnswerRow:
    """The bits of a `field_answer` the review assembly needs."""

    id: UUID
    field_path: str
    value: Any  # stored JSONB, e.g. {"value": ...}
    source: str
    confidence: int | None
    evidence: str | None


def unwrap_value(stored: Any) -> Any:
    """Field answers persist as `{"value": <raw>}`; return the raw value (pass other
    shapes through unchanged)."""
    if isinstance(stored, dict) and "value" in stored:
        return stored["value"]
    return stored


# Strip only ASCII whitespace (not the default `str.strip()`, which also folds Unicode
# whitespace like U+00A0). A deliberately conservative, stable rule: a non-ASCII space is
# retained, so it still counts as a real value difference.
_ASCII_WHITESPACE = " \t\n\r\f\v"


def strip_answer(value: str) -> str:
    """Trim an answer's padding under the one whitespace rule `normalize_value` folds by."""
    return value.strip(_ASCII_WHITESPACE)


def normalize_value(value: Any) -> Any:
    """Canonicalize a value for dispute comparison: strings are stripped (ASCII whitespace
    only) + lowercased so case- and whitespace-only differences are not disputes;
    non-strings (numbers, bools, null, objects) pass through unchanged. This is the sole
    dispute-normalization rule — both the detail view and the complete-gate/resolve count
    go through `is_disputed` / `build_field_views`, so there is no second (SQL)
    implementation to keep in sync."""
    if isinstance(value, str):
        return value.strip(_ASCII_WHITESPACE).lower()
    return value


def is_blank_answer(value: Any) -> bool:
    """True when a value counts as "not answered": None or whitespace-only. A blank
    AI answer must never supersede a baseline — it would demote the real value and
    flag an empty field as a dispute (VR2-93). Single definition for every AI write
    path; humans/intake may still clear a value."""
    return value is None or not str(value).strip()


def dispute_view(
    *,
    source: str,
    value: Any,
    confidence: int | None,
    baseline_value: Any,
) -> dict[str, Any] | None:
    """The `{previous_value, current_value, confidence, reasoning}` payload for one field, or
    `None` when it is not disputed. Evidence is deliberately absent: it belongs to the answer,
    not the divergence, so it rides on the field view instead."""
    if source != AnswerSource.AI_CALL.value:
        return None
    if normalize_value(unwrap_value(value)) == normalize_value(unwrap_value(baseline_value)):
        return None
    return {
        "previous_value": unwrap_value(baseline_value),
        "current_value": unwrap_value(value),
        "confidence": confidence,
        "reasoning": None,
    }


def is_disputed(current: AnswerRow, baseline_value: Any) -> bool:
    """True when the current value came from the AI call and diverges from the
    human/intake baseline. Thin wrapper over `dispute_view` so there is one rule."""
    return (
        dispute_view(
            source=current.source,
            value=current.value,
            confidence=current.confidence,
            baseline_value=baseline_value,
        )
        is not None
    )


def all_required_paths(schema_json: Mapping[str, Any]) -> list[str]:
    """Dotted paths of every `required_state == "required"` field across all sections."""
    paths: list[str] = []
    for section in schema_json.get("sections", []):
        section_key = section.get("section_key", "")
        for field_key, field_def in (section.get("properties") or {}).items():
            if isinstance(field_def, dict) and field_def.get("required_state") == "required":
                paths.append(f"{section_key}.{field_key}")
    return sorted(paths)


def completion_pct(filled_paths: Collection[str], schema_json: Mapping[str, Any]) -> float:
    """Percentage (0-100, 2 dp) of required fields that have a value."""
    required = all_required_paths(schema_json)
    if not required:
        return 0.0
    filled = sum(1 for path in required if path in filled_paths)
    return round(filled / len(required) * 100, 2)


def completion_pct_v2(values: Mapping[str, Any], schema_json: Mapping[str, Any]) -> float:
    """DSL 2.x completion (0-100, 2 dp): required ∧ applicable ∧ COLLECTABLE leaves, evaluated
    against the current answer values (`applicable_when` chains from the section down,
    `required: bool | {when}`). A leaf with a declared `default` counts as filled — display/export
    assume it (spec §4.4). Mirrors the frontend's `completionPercent`.

    Restricted to `ask`/`confirm` because only those can be filled BY A CALL, and this number gates
    the `low_fill` retry decision in `post_call.py`. Every non-askable required leaf is also a
    `required_intake_fields` target, so it is always filled and contributed a constant offset that
    no call could move — a brand-new form read as a third complete (spec D9)."""
    doc = FormSchemaDoc.model_validate(schema_json)
    shared = doc.shared_conditions or {}
    relevant = [
        (path, leaf)
        for path, leaf, gates in leaf_gates(doc)
        if leaf.role in COLLECTED_ROLES
        and is_applicable(gates, values, shared)
        and is_required(leaf, values, shared)
    ]
    if not relevant:
        return 100.0
    alternatives = alternative_index(alternative_pairs(doc))
    filled = sum(
        1 for path, leaf in relevant if is_satisfied(path, leaf.default, values, alternatives)
    )
    return round(filled / len(relevant) * 100, 2)


def form_completion_pct(values: Mapping[str, Any], schema_json: Mapping[str, Any]) -> float:
    """Version-gated completion %: v2 evaluates conditions against the values;
    v1 only needs which paths are filled."""
    if is_v2(schema_json):
        return completion_pct_v2(values, schema_json)
    return completion_pct(set(values), schema_json)


def adjudication_action(new_value: Any, current_value: Any, prior_values: Collection[Any]) -> str:
    """Which `DisputeActionType` a human edit represents: ACCEPT (unchanged),
    OVERRIDE (reverted to a known prior value), else CORRECT (a fresh value)."""
    if new_value == current_value:
        return DisputeActionType.ACCEPT.value
    if new_value in prior_values:
        return DisputeActionType.OVERRIDE.value
    return DisputeActionType.CORRECT.value


def build_field_views(
    current_answers: Iterable[AnswerRow],
    baseline_value_by_path: Mapping[str, Any],
    *,
    call_scoped_paths: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Assemble the flat, dotted-path field views the detail endpoint returns. Each
    item is `{field_path, value, source, confidence, evidence, dispute}`; `dispute` is
    non-null only when the current AI value diverges from the human/intake baseline.

    `evidence` is top-level rather than dispute-nested precisely because a `dispute` is
    absent whenever the AI value AGREES with the baseline — those answers still have
    evidence worth reviewing.

    `baseline_value_by_path` maps a field path to its most recent intake/human stored
    value (`{"value": ...}`); a missing entry means no baseline (treated as `None`).

    `call_scoped_paths` (the schema's `collected_per="call"` leaves) are never disputed: their
    value describes ONE CALL, so there is no form-level baseline for it to diverge from and never
    will be. Without this, the rep's name and the call reference number are flagged on every call
    with `previous_value: null` forever. `evidence` still rides on the view — an answer with no
    dispute can still have evidence worth reading."""
    exempt = set(call_scoped_paths)
    views: list[dict[str, Any]] = []
    for answer in sorted(current_answers, key=lambda a: a.field_path):
        dispute = (
            None
            if answer.field_path in exempt
            else dispute_view(
                source=answer.source,
                value=answer.value,
                confidence=answer.confidence,
                baseline_value=baseline_value_by_path.get(answer.field_path),
            )
        )
        views.append(
            {
                "field_path": answer.field_path,
                "value": unwrap_value(answer.value),
                "source": answer.source,
                "confidence": answer.confidence,
                "evidence": answer.evidence,
                "dispute": dispute,
            }
        )
    return views


@dataclass(frozen=True)
class FieldStatus:
    """Immutable snapshot of a filled field's satisfaction state: source, AI confidence, and the
    call that produced it. An unfilled field has no status at all (absent from the map).

    `call_id` is NULL for intake/human answers. It is what lets `is_call_confirmed` ask whether an
    AUTHORITATIVE call produced this value — see spec D8."""

    source: str | None
    ai_supported: bool | None
    ai_confidence: int | None
    call_id: UUID | None = None


def is_field_satisfied(status: FieldStatus | None, *, floor: int) -> bool:
    """True when a field's status meets retry-gate requirements: human/intake-sourced
    (trusted), or AI-sourced with supported language and confidence >= floor.
    ``None`` means the field is unfilled — never satisfied."""
    if status is None:
        return False
    if status.source in (AnswerSource.INTAKE.value, AnswerSource.HUMAN.value):
        return True
    if status.source == AnswerSource.AI_CALL.value:
        return bool(status.ai_supported) and (status.ai_confidence or 0) >= floor
    return True  # unknown source but filled — treat as satisfied


def is_call_confirmed(
    status: FieldStatus | None, *, authoritative_calls: Collection[UUID], floor: int
) -> bool:
    """True only when an AUTHORITATIVE call collected this value and the judge supported it.

    The retry ask set's rule, and deliberately stricter than `is_field_satisfied`: an intake or
    human value is trusted for completeness and for the retry-WORTHINESS decision, but it was never
    put to the payer's representative, so a genuine retry still owes it. Answers from a call that
    captured no reference number are not proof either — see spec D8.

    This is `gating_seed`'s rule (an ask-role value on file is a pre-call baseline, never an answer)
    applied to the focus set, which is computed from `field_answer` and had no equivalent guard.
    """
    if status is None or status.source != AnswerSource.AI_CALL.value:
        return False
    if status.call_id is None or status.call_id not in authoritative_calls:
        return False
    return bool(status.ai_supported) and (status.ai_confidence or 0) >= floor


def _required_paths(
    schema_json: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    askable_only: bool,
    include_defaulted: bool = False,
) -> list[str]:
    """Paths of required, applicable leaves that could still owe an answer — optionally only
    collectible (ask/confirm) ones. v2: filters by role + applicability. v1: returns all required
    paths (no role concept).

    A leaf declaring a `default` is excluded by default: `completion_pct_v2` counts it filled and
    the export writes it, so leaving it here would block auto-completion on a field the form calls
    done. `include_defaulted=True` is the RETRY ASK SET only, which follows `owed_now` — a default
    declares the value a field takes when not collected, never that the question need not be asked.
    """
    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        shared = doc.shared_conditions or {}
        return [
            path
            for path, leaf, gates in leaf_gates(doc)
            if (not askable_only or leaf.role in COLLECTED_ROLES)
            and (include_defaulted or leaf.default is None)
            and is_applicable(gates, values, shared)
            and is_required(leaf, values, shared)
        ]
    return all_required_paths(schema_json)


def _alternatives(schema_json: Mapping[str, Any]) -> AlternativeIndex:
    """The either/or index; empty for v1, which has no `alternatives` concept."""
    if not is_v2(schema_json):
        return {}
    return alternative_index(alternative_pairs(FormSchemaDoc.model_validate(schema_json)))


def _confirm_paths(schema_json: Mapping[str, Any]) -> frozenset[str]:
    """Paths of `role="confirm"` leaves. Their declared purpose is payer CONFIRMATION, so the
    intake value is the thing to be confirmed, not the confirmation (spec §4.1). Empty for v1,
    which has no role concept."""
    if not is_v2(schema_json):
        return frozenset()
    doc = FormSchemaDoc.model_validate(schema_json)
    return frozenset(path for path, leaf in doc.leaf_items() if leaf.role == "confirm")


def _satisfied(
    path: str,
    status_by_path: Mapping[str, FieldStatus],
    alternatives: AlternativeIndex,
    *,
    floor: int,
    confirm_paths: Collection[str] = (),
) -> bool:
    """Satisfied itself, or by a sibling in its either/or group — one answer satisfies the pair.

    A `confirm`-role leaf is NOT satisfied by intake alone (spec §4.1); a human edit still is,
    since that is a reviewer's deliberate decision. The rule is applied to the leaf itself and
    not to its siblings: no confirm leaf is an either/or member in any shipped catalog, pinned
    by `test_no_confirm_leaf_is_an_either_or_member_in_any_shipped_catalog`.

    Judged with `is_field_satisfied` rather than `conditions.has_value` because `_gate_values` may
    hand these callers a sentinel map instead of real values, and because a low-confidence AI
    answer must not satisfy its sibling any more than it satisfies itself."""
    status = status_by_path.get(path)
    if path in confirm_paths and status is not None and status.source == AnswerSource.INTAKE.value:
        own = False
    else:
        own = is_field_satisfied(status, floor=floor)
    return own or any(
        is_field_satisfied(status_by_path.get(other), floor=floor)
        for other in alternatives.get(path, ())
    )


def _unsatisfied(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    values: Mapping[str, Any] | None,
    *,
    floor: int,
    askable_only: bool,
) -> tuple[list[str], list[str]]:
    """`(applicable required paths, those still unsatisfied)` — the set `unsatisfied_required_paths`
    and `retryable_required_paths` both measure, so the auto-complete gate and the retry set cannot
    drift apart. `satisfied_required_fraction` (verified_pct) no longer routes through this: it
    judges by `is_call_confirmed`, not `is_field_satisfied` — see its own docstring for why."""
    gate_values = _gate_values(status_by_path, values)
    applicable = _required_paths(schema_json, gate_values, askable_only=askable_only)
    alternatives = _alternatives(schema_json)
    confirm_paths = _confirm_paths(schema_json)
    return applicable, [
        path
        for path in applicable
        if not _satisfied(
            path, status_by_path, alternatives, floor=floor, confirm_paths=confirm_paths
        )
    ]


def _gate_values(
    status_by_path: Mapping[str, FieldStatus], values: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """The values conditions evaluate against. With *values* (the form's real
    current answers — PHI, so only in-session callers pass them) gates evaluate
    exactly. Without, a sentinel stands in for each filled field (PHI-free):
    presence-based gates evaluate exactly; a value-comparing gate (``eq``/``in``…)
    sees the sentinel and reads as "not matching", so its dependents are treated
    as inapplicable — a deliberate conservative approximation for the dispatcher's
    retry nudge, never for an authoritative status decision."""
    return values if values is not None else dict.fromkeys(status_by_path, "x")


def unsatisfied_required_paths(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
) -> list[str]:
    """Paths of required, applicable fields (ANY role) that are not yet satisfied.
    The authoritative completeness check: a form may only auto-COMPLETE when this
    is empty — an unsatisfied non-askable field can never be fixed by a retry
    call, so it must route to human review instead."""
    return _unsatisfied(status_by_path, schema_json, values, floor=floor, askable_only=False)[1]


def retryable_required_paths(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
) -> list[str]:
    """Paths of required, applicable, askable fields that are not yet satisfied.
    These are the fields a retry call should attempt to fill. See _gate_values
    for the values-vs-sentinel evaluation contract."""
    return _unsatisfied(status_by_path, schema_json, values, floor=floor, askable_only=True)[1]


def satisfied_required_fraction(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
    authoritative_calls: Collection[UUID],
) -> float:
    """Fraction (0.0-1.0) of required, applicable, COLLECTABLE leaves an AUTHORITATIVE call
    confirmed — what `verified_pct` reports and what the park gate compares against
    `tenant.retry_fill_threshold`.

    Two restrictions, and they only work together (spec D9):

    * `is_call_confirmed`, not `is_field_satisfied` — an intake value is trusted for completeness
      and for the retry-worthiness decision but was never put to the payer, and an answer from a
      call that captured no reference number is not proof either;
    * `askable_only=True` — the never-collectable leaves would otherwise stay in the denominator
      while becoming permanently unsatisfiable, capping this below 100% on a form no retry could
      ever finish clearing.

    NOT the auto-complete gate: `unsatisfied_required_paths` keeps `is_field_satisfied` and all
    roles, because a human signing a form off legitimately trusts an intake-supplied patient name.
    """
    gate_values = _gate_values(status_by_path, values)
    applicable = _required_paths(schema_json, gate_values, askable_only=True)
    if not applicable:
        return 1.0
    alternatives = _alternatives(schema_json)
    confirmed = sum(
        1
        for path in applicable
        if _confirmed(path, status_by_path, alternatives, authoritative_calls, floor=floor)
    )
    return confirmed / len(applicable)


def field_labels(schema_json: Mapping[str, Any], paths: Sequence[str]) -> list[str]:
    """Human-readable labels for field paths: leaf titles in v2, else the paths themselves."""
    if not is_v2(schema_json):
        return list(paths)
    doc = FormSchemaDoc.model_validate(schema_json)
    titles = {path: leaf.title for path, leaf, _ in leaf_gates(doc)}
    return [titles.get(p, p) for p in paths]


def expand_to_groups(doc: FormSchemaDoc, paths: Collection[str]) -> list[str]:
    """Grow *paths* so a field inside a group pulls in ALL collectable leaves of
    that group (a partial group reads oddly on a call). For each group whose
    subtree contains a wanted path, every collectable (ask/confirm) leaf under it
    joins the set. Returns the union in document order; a path in no group passes
    through unchanged."""
    collectable = doc.collection_paths()  # ask/confirm leaves, document order
    collectable_set = set(collectable)
    wanted = set(paths)
    result = set(wanted)
    for group_path in doc.group_paths():
        prefix = f"{group_path}."
        if any(p == group_path or p.startswith(prefix) for p in wanted):
            result.update(p for p in collectable if p.startswith(prefix))
    ordered = [p for p in collectable if p in result]
    ordered.extend(p for p in paths if p not in collectable_set)
    return ordered


def focus_paths(
    doc: FormSchemaDoc,
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any],
    authoritative_calls: Collection[UUID],
) -> list[str]:
    """Every path a FOCUSED retry should put to the representative, in document order.

    Three sources, unioned:

    * required, applicable, askable leaves no AUTHORITATIVE call confirmed (`is_call_confirmed`) —
      which covers never-collected, judge-rejected, intake-supplied-but-never-confirmed, and
      collected-by-an-unverifiable-call alike;
    * every collectable leaf of a group any of those falls inside (`expand_to_groups`) — a partly
      re-asked panel reads oddly on a call;
    * every `collected_per="call"` leaf, whatever is on file — the rep's name and the call reference
      number describe THIS call, and keeping them is also what retains the greeting and wrap-up
      tasks, since `focus_call_plan` drops a task with no kept fields.

    Defaulted leaves are included: this is the ask set, and a `default` declares the value a field
    takes when not collected, never that the question need not be asked (`owed_now`).

    NOT the retry-worthiness decision — `retryable_required_paths` still answers that, and must
    keep excluding call-scoped and defaulted leaves or a form whose only gaps are unaskable would
    redial to no benefit (spec D3). Values are PHI; never log them.
    """
    applicable = _required_paths(schema_json, values, askable_only=True, include_defaulted=True)
    alternatives = _alternatives(schema_json)
    owed = [
        path
        for path in applicable
        if not _confirmed(path, status_by_path, alternatives, authoritative_calls, floor=floor)
    ]
    wanted = set(expand_to_groups(doc, owed)) | doc.collected_per_call_paths()
    ordered = [path for path in doc.collection_paths() if path in wanted]
    ordered.extend(sorted(wanted.difference(ordered)))
    return ordered


def _confirmed(
    path: str,
    status_by_path: Mapping[str, FieldStatus],
    alternatives: AlternativeIndex,
    authoritative_calls: Collection[UUID],
    *,
    floor: int,
) -> bool:
    """Confirmed itself, or by a sibling in its either/or group — one answer satisfies the pair.
    Mirrors `_satisfied`, swapping in the authoritative-call rule."""
    if is_call_confirmed(
        status_by_path.get(path), authoritative_calls=authoritative_calls, floor=floor
    ):
        return True
    return any(
        is_call_confirmed(
            status_by_path.get(other), authoritative_calls=authoritative_calls, floor=floor
        )
        for other in alternatives.get(path, ())
    )
