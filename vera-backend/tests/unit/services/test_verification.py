"""`load_verified_fraction` — the one seam both retry gates read (spec E3).

Session faked exactly like `test_queue_dispatcher.py`'s `FakeSession`: this module drives the
identical `load_field_status` / `load_authoritative_call_ids` / `current_values_by_path` combo
(queue_dispatcher's focused-retry read), so routing `execute()` by entity + column shape is the
established seam rather than a new one.
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
from vera_core.forms.intake import required_intake_fields
from vera_core.forms.review import retryable_required_paths
from vera_core.models import PatientForm, SchemaVersion
from vera_core.models.enums import AnswerSource, FormStatus
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


def _form(**overrides: Any) -> PatientForm:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "schema_version_id": uuid4(),
        "status": FormStatus.AI_PROCESSING.value,
        "patient_name": "Jane Doe",
        "insurance_provider_phone_number": "+15551234567",
        "retry_count": 0,
        "completion_pct": 100.0,
        "enqueued_at": None,
    }
    defaults.update(overrides)
    return PatientForm(**defaults)


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
    assert await load_verified_fraction(_session(V1_SCHEMA), _form(), floor=70) is None


@pytest.mark.asyncio
async def test_a_call_with_no_reference_number_verifies_nothing() -> None:
    """Spec S3: the same answers read completion 100% and verified 0%."""
    fraction = await load_verified_fraction(
        _session(IBV_STANDARD_V2, answers=_all_askable_answered(authoritative=False)),
        _form(),
        floor=70,
    )
    assert fraction == 0.0
