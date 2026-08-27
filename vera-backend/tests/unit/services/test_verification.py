"""`load_verified_fraction` — the dispute-resolve gate's number (spec E3).

Session faked exactly like `test_queue_dispatcher.py`'s `FakeSession`: this module drives the
identical `load_field_status` / `load_authoritative_call_ids` pair (queue_dispatcher's
focused-retry read), so routing `execute()` by entity + column shape is the established seam
rather than a new one. The doc, its raw json and the values are passed in by the caller, so
`_fraction` below assembles them the way `resolve_disputes` does.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.services.stmt_fakes import bound_value as _bound_value
from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc, PromotedFields
from vera_core.forms.intake import required_intake_fields
from vera_core.forms.review import retryable_required_paths
from vera_core.models import SchemaVersion
from vera_core.models.enums import AnswerSource
from vera_core.services.verification import load_verified_fraction

FLOOR = 70

_FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
IBV_STANDARD_V2: dict[str, Any] = json.loads(
    (_FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text()
)
REFERENCE_FIELD: str = IBV_STANDARD_V2["rep_call_reference_number_field"]

# No `dsl_version` starting "2." — is_v2() reads this as legacy, which is the only thing
# `load_verified_fraction` inspects before returning None; nothing else about it matters.
V1_SCHEMA: dict[str, Any] = {"dsl_version": "1.0"}


@dataclass
class _Answer:
    """One `field_answer` row, shaped to feed exactly the three query results
    `load_verified_fraction` reads (field_path+value, the 6-column status row, bare call_id)."""

    field_path: str
    value: Any
    source: str
    call_id: UUID | None = None
    confidence: int | None = None
    supported: bool | None = None
    eval_confidence: int | None = None
    is_current: bool = True


class _Result:
    """Stand-in for a SQLAlchemy `Result` — only the accessors the readers call."""

    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows if rows is not None else []

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeSession:
    """Routes `execute()` by entity + column shape — SchemaVersion's `schema_json` scalar,
    then the three FieldAnswer-entity queries distinguished by their selected column names
    (all three share the entity, so name-routing is the only way to tell them apart)."""

    def __init__(self, schema_json: dict[str, Any], *, answers: Sequence[_Answer] = ()) -> None:
        self._schema_json = schema_json
        self._answers = list(answers)

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0].get("entity")
        if entity is SchemaVersion:
            return _Result(scalar=self._schema_json)
        names = [c["name"] for c in stmt.column_descriptions]
        if names == ["field_path", "value"]:
            return _Result(rows=[(a.field_path, a.value) for a in self._answers if a.is_current])
        if names == ["call_id"]:
            reference_field = _bound_value(stmt, "field_path")
            return _Result(
                rows=[
                    a.call_id
                    for a in self._answers
                    if a.field_path == reference_field and a.call_id is not None
                ]
            )
        return _Result(  # load_field_status's 6-column status row
            rows=[
                (a.field_path, a.source, a.confidence, a.call_id, a.supported, a.eval_confidence)
                for a in self._answers
                if a.is_current
            ]
        )


def _session(schema_json: dict[str, Any], *, answers: Sequence[_Answer] = ()) -> AsyncSession:
    """Cast to the real type — mypy-strict clean, matching `test_queue_dispatcher.py`'s
    `cast(AsyncSession, fake)` convention."""
    return cast(AsyncSession, _FakeSession(schema_json, answers=answers))


async def _fraction(
    schema_json: dict[str, Any],
    *,
    answers: Sequence[_Answer] = (),
    floor: int = FLOOR,
    values: dict[str, Any] | None = None,
) -> float | None:
    """Call the loader the way `resolve_disputes` does: the parsed doc (None for a legacy v1
    schema), its raw json, and the post-write values it already holds. *values* defaults to the
    answers' own values; pass it to model a caller handing over the wrong map."""
    doc = FormSchemaDoc.model_validate(schema_json) if is_v2(schema_json) else None
    if values is None:
        values = {a.field_path: a.value for a in answers if a.is_current}
    return await load_verified_fraction(
        _session(schema_json, answers=answers),
        uuid4(),
        floor=floor,
        doc=doc,
        schema_json=schema_json,
        values=values,
    )


def _population(schema_json: dict[str, Any], *, floor: int) -> list[str]:
    """The required, applicable, askable leaves — a fixed point over `retryable_required_paths`
    so a leaf gated on another leaf's value (not just an intake value) still lands in scope."""
    values: dict[str, Any] = dict.fromkeys(required_intake_fields(schema_json), "x")
    paths = retryable_required_paths({}, schema_json, floor=floor, values=values)
    for _ in range(5):
        values = dict(values) | dict.fromkeys(paths, "x")
        next_paths = retryable_required_paths({}, schema_json, floor=floor, values=values)
        if set(next_paths) == set(paths):
            return next_paths
        paths = next_paths
    raise AssertionError("population did not converge — check for a gate cycle")


def _all_askable_answered(*, authoritative: bool) -> list[_Answer]:
    """Every required, applicable, askable leaf of `IBV_STANDARD_V2` answered by one AI call —
    intake fields too, so the gates the population itself depends on evaluate consistently.
    `authoritative=False` leaves the reference-number leaf with no answer row at all (the rep
    gave none), so no call ever lands in `load_authoritative_call_ids`'s result."""
    call_id = uuid4()
    intake_paths = required_intake_fields(IBV_STANDARD_V2)
    answers = [_Answer(path, "x", source=AnswerSource.INTAKE.value) for path in intake_paths]
    for path in _population(IBV_STANDARD_V2, floor=FLOOR):
        if path == REFERENCE_FIELD and not authoritative:
            continue
        answers.append(
            _Answer(
                path,
                "x",
                source=AnswerSource.AI_CALL.value,
                call_id=call_id,
                confidence=95,
                supported=True,
                eval_confidence=95,
            )
        )
    return answers


@pytest.mark.asyncio
async def test_returns_none_for_a_legacy_v1_schema() -> None:
    """v1 declares no rep_call_reference_number_field, so there is nothing to be authoritative
    ABOUT — the caller must fall back rather than read 0.0 as 'nothing verified'."""
    assert await _fraction(V1_SCHEMA) is None


@pytest.mark.asyncio
async def test_a_call_with_no_reference_number_verifies_nothing() -> None:
    """Spec S3: the same answers read completion 100% and verified 0%."""
    fraction = await _fraction(IBV_STANDARD_V2, answers=_all_askable_answered(authoritative=False))
    assert fraction == 0.0


@pytest.mark.asyncio
async def test_a_call_with_a_reference_number_verifies_everything() -> None:
    """The mirror of the above: every required, applicable, askable leaf answered by a
    call that also captured the reference number is fully verified, not just non-zero —
    this is the branch a mutation returning a constant 0.0 for every v2 form would still
    pass unless this exact assertion runs it."""
    fraction = await _fraction(IBV_STANDARD_V2, answers=_all_askable_answered(authoritative=True))
    assert fraction == 1.0


# A required askable leaf gated on ANOTHER field's VALUE — the one shape that makes the
# caller-supplied `values` map change the answer. Neither shipped catalog has one today
# (both read the same population against `{}` and against seeded intake), which is why this
# contract needs a schema of its own rather than a catalog.
_GATED = "sections.cov.secondary_payer_name"
_GATE = "sections.cov.cob_status"
VALUE_GATED_V2: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "Value gated",
    "insurance_type": "infertility_treatment",
    "system_fields": {"network_status": _GATE},
    "rep_call_reference_number_field": _GATE,
    "promoted_fields": dict.fromkeys(PromotedFields.model_fields, _GATE),
    "sections": {
        "cov": {
            "title": "Coverage",
            "role": "collect",
            "fields": {
                "cob_status": {
                    "type": "text",
                    "title": "COB status",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "Is there coordination of benefits?"},
                },
                "secondary_payer_name": {
                    "type": "text",
                    "title": "Secondary payer name",
                    "role": "ask",
                    "required": True,
                    "applicable_when": {"field": _GATE, "op": "eq", "value": "Yes"},
                    "prompt": {"ask": "Who is the secondary payer?"},
                },
            },
        }
    },
    "tasks": [{"task_key": "t1", "title": "Task 1", "sections": ["cov"]}],
}


@pytest.mark.asyncio
async def test_the_values_the_caller_passes_decide_which_leaves_count() -> None:
    """The loader no longer reads values itself — it trusts the caller — so the map it is
    handed has to be shown to change the answer, or a call site passing a stale or empty one
    would be a silently wrong number rather than a failing test.

    `values` IS the gate-evaluation map (`review._gate_values`): with the real answers the
    gated leaf is applicable and unconfirmed, so the form is half verified; with an empty map
    the gate reads as unmatched, the leaf leaves the denominator, and the same form reads
    fully verified.
    """
    call_id = uuid4()
    answers = [
        _Answer(
            _GATE,
            "Yes",
            source=AnswerSource.AI_CALL.value,
            call_id=call_id,
            confidence=95,
            supported=True,
            eval_confidence=95,
        )
    ]

    honest = await _fraction(VALUE_GATED_V2, answers=answers)
    assert honest == 0.5  # gate confirmed, gated leaf owed

    stale = await _fraction(VALUE_GATED_V2, answers=answers, values={})
    assert stale == 1.0  # gated leaf never counted
