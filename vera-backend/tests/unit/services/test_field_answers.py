"""Unit tests for the ai_call answer writer's supersede + idempotency logic.

`record_answer` is exercised against a minimal fake session (no live DB): it routes the
current-answer SELECT and records demote/flush/insert so the merge invariant (one current
row, cleared before the new insert) and the at-least-once no-op are observable directly.
"""

from typing import Any
from uuid import uuid4

import pytest

from vera_core.models import FieldAnswer
from vera_core.models.enums import AnswerSource
from vera_core.services.field_answers import record_answer

TENANT, FORM, CALL = uuid4(), uuid4(), uuid4()


class _Result:
    def __init__(self, scalar: Any) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _FakeSession:
    """Returns a fixed current FieldAnswer for the SELECT; records add()/flush() order
    so a test can assert the old current was demoted+flushed before the new insert."""

    def __init__(self, current: FieldAnswer | None) -> None:
        self.current = current
        self.events: list[str] = []
        self.added: list[FieldAnswer] = []

    async def execute(self, stmt: Any) -> _Result:
        return _Result(self.current)

    async def flush(self) -> None:
        self.events.append("flush")

    def add(self, obj: FieldAnswer) -> None:
        self.events.append("add")
        self.added.append(obj)


def _current(
    value: Any,
    *,
    source: str = AnswerSource.AI_CALL.value,
    evidence_seq: int | None = None,
) -> FieldAnswer:
    return FieldAnswer(
        tenant_id=TENANT,
        form_id=FORM,
        call_id=CALL,
        field_path="sections.a.x",
        value={"value": value},
        source=source,
        evidence_seq=evidence_seq,
        is_current=True,
    )


async def _record(session: _FakeSession, value: Any, *, evidence_seq: int | None = 3) -> bool:
    return await record_answer(
        session,  # type: ignore[arg-type]
        tenant_id=TENANT,
        form_id=FORM,
        call_id=CALL,
        field_path="sections.a.x",
        raw_value=value,
        source=AnswerSource.AI_CALL.value,
        confidence=90,
        evidence_seq=evidence_seq,
    )


@pytest.mark.asyncio
async def test_insert_when_no_current_answer() -> None:
    session = _FakeSession(current=None)
    assert await _record(session, "Yes") is True
    assert session.events == ["add"]  # nothing to demote
    assert session.added[0].is_current is True
    assert session.added[0].source == AnswerSource.AI_CALL.value


@pytest.mark.asyncio
async def test_identical_redelivery_is_a_noop() -> None:
    session = _FakeSession(current=_current("Yes"))
    assert await _record(session, "Yes") is False  # same source/call/value
    assert session.events == []  # no write


@pytest.mark.asyncio
async def test_changed_value_supersedes_current_before_insert() -> None:
    current = _current("Yes")
    session = _FakeSession(current=current)
    assert await _record(session, "No") is True
    # demote → flush (clear old current for fa_current_uq) → insert new, in that order
    assert session.events == ["flush", "add"]
    assert current.is_current is False
    assert session.added[0].value == {"value": "No"}


@pytest.mark.asyncio
async def test_same_value_but_different_source_supersedes() -> None:
    # an intake value equal to the ai_call value is still superseded (source differs)
    session = _FakeSession(current=_current("Yes", source=AnswerSource.INTAKE.value))
    assert await _record(session, "Yes") is True
    assert session.events == ["flush", "add"]


@pytest.mark.asyncio
async def test_stale_redelivery_does_not_overwrite_a_newer_answer() -> None:
    # The reordering the per-room dispatch can't prevent: answer A (seq 2) is left unacked
    # because its Call row hadn't committed; answer B (seq 7) for the same field lands; A is
    # then redelivered. Without the evidence_seq guard A's older value would supersede B's.
    session = _FakeSession(current=_current("Yes, fully met", evidence_seq=7))
    assert await _record(session, "Not sure", evidence_seq=2) is False
    assert session.events == []  # no demote, no insert


@pytest.mark.asyncio
async def test_same_seq_re_extraction_still_supersedes() -> None:
    # A retried pass over the SAME turn may refine its value (the observer re-arms and
    # retries after a provider outage) — that is news, not a stale replay.
    session = _FakeSession(current=_current("ABC", evidence_seq=5))
    assert await _record(session, "ABC123", evidence_seq=5) is True
    assert session.events == ["flush", "add"]


@pytest.mark.asyncio
async def test_newer_seq_supersedes_as_normal() -> None:
    session = _FakeSession(current=_current("Yes", evidence_seq=2))
    assert await _record(session, "No", evidence_seq=9) is True
    assert session.added[0].evidence_seq == 9


@pytest.mark.asyncio
async def test_human_resolve_supersedes_regardless_of_seq() -> None:
    # A different source is a different authority: a human correction must land even though
    # it carries no evidence_seq at all and the current ai_call row has a high one.
    session = _FakeSession(current=_current("Yes", evidence_seq=99))
    wrote = await record_answer(
        session,  # type: ignore[arg-type]
        tenant_id=TENANT,
        form_id=FORM,
        call_id=CALL,
        field_path="sections.a.x",
        raw_value="No",
        source=AnswerSource.HUMAN.value,
        evidence_seq=None,
    )
    assert wrote is True


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_blank_ai_value_never_supersedes(blank: Any) -> None:  # VR2-93
    current = _current("W12345", source=AnswerSource.INTAKE.value)
    session = _FakeSession(current=current)
    assert await _record(session, blank) is False
    assert session.events == []
    assert current.is_current is True
