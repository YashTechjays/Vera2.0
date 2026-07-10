"""Unit tests for the transcript finalizer (Task 16).

`_build_rows` is pure (role->source mapping, seq ordering, spoke_at) and needs no
fakes. `finalize_transcript` is exercised against a minimal in-memory call-stream
double and a fake `tenant_session` (mirrors the DB-seam fakes in
test_worker_events.py) — the real ON CONFLICT / RLS behavior is covered by the
integration test against a live Postgres.
"""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

import control_plane.transcript_finalizer as finalizer_mod
from control_plane.transcript_finalizer import _build_rows, finalize_transcript
from vera_core.call_stream import CallStreamEvent, CallStreamService
from vera_core.models.enums import TranscriptSource
from vera_core.observability.correlation import RoomRef

_ROOM = "call--x--y"
_SESSIONMAKER = cast("async_sessionmaker[AsyncSession]", object())


def _ref() -> RoomRef:
    return RoomRef(tenant_id=uuid4(), call_id=uuid4())


# ---------------------------------------------------------------------------
# _build_rows — pure mapping, no fakes needed.
# ---------------------------------------------------------------------------


def test_build_rows_maps_roles_to_sources_and_orders_seq() -> None:
    ref = _ref()
    events = [
        CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1000),
        CallStreamEvent(type="transcript", data={"role": "agent", "text": "hello"}, ts=2000),
    ]
    rows, skipped = _build_rows(ref, events)
    assert skipped == 0
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["tenant_id"] == ref.tenant_id
    assert rows[0]["call_id"] == ref.call_id
    assert rows[0]["source"] == TranscriptSource.REP.value  # user == the payer rep
    assert rows[0]["role"] == "user"
    assert rows[0]["message"] == "hi"
    assert rows[0]["spoke_at"] == datetime.fromtimestamp(1.0, tz=UTC)
    assert rows[1]["source"] == TranscriptSource.BOT.value  # agent == Vera


def test_build_rows_skips_non_transcript_envelopes_without_consuming_a_seq() -> None:
    ref = _ref()
    events = [
        CallStreamEvent(type="call_status", data={"status": "active"}, ts=1),
        CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=2),
    ]
    rows, skipped = _build_rows(ref, events)
    assert skipped == 0
    assert len(rows) == 1
    assert rows[0]["seq"] == 0  # the call_status envelope never occupies a seq slot


def test_build_rows_skips_unknown_role_with_a_counted_skip() -> None:
    """An unrecognized role can only come from a corrupted envelope — mapping it to
    BOT would misattribute speech that may not be the agent's, so it is dropped and
    counted rather than guessed at."""
    ref = _ref()
    events = [
        CallStreamEvent(type="transcript", data={"role": "supervisor", "text": "??"}, ts=1),
        CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=2),
    ]
    rows, skipped = _build_rows(ref, events)
    assert skipped == 1
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0]["seq"] == 0  # the skipped row never occupies a seq slot either


def test_build_rows_on_empty_events_returns_nothing() -> None:
    assert _build_rows(_ref(), []) == ([], 0)


# ---------------------------------------------------------------------------
# finalize_transcript — DB/stream wiring around _build_rows.
# ---------------------------------------------------------------------------


class _FakeCallStream:
    """In-memory call-stream double: read_all returns a fixed snapshot until
    clear() is called, mirroring a real Redis DEL."""

    def __init__(self, events: list[CallStreamEvent]) -> None:
        self._events = events
        self.cleared: list[str] = []

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return self._events

    async def clear(self, room_name: str) -> None:
        self.cleared.append(room_name)
        self._events = []


class _FakeSession:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.executed: list[Any] = []
        self._error = error

    async def execute(self, stmt: Any) -> None:
        if self._error is not None:
            raise self._error
        self.executed.append(stmt)


class _FakeSessionCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_tenant_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(finalizer_mod, "tenant_session", lambda sm, tid: _FakeSessionCtx(session))


async def _finalize(stream: _FakeCallStream, ref: RoomRef, room: str = _ROOM) -> int:
    return await finalize_transcript(_SESSIONMAKER, cast(CallStreamService, stream), ref, room)


@pytest.mark.asyncio
async def test_finalize_writes_rows_and_clears_stream_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeCallStream(
        [CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1)]
    )
    session = _FakeSession()
    _patch_tenant_session(monkeypatch, session)

    count = await _finalize(stream, _ref())

    assert count == 1
    assert len(session.executed) == 1
    assert isinstance(session.executed[0], Insert)
    assert stream.cleared == [_ROOM]


@pytest.mark.asyncio
async def test_finalize_on_stream_with_no_transcript_turns_writes_nothing_but_still_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers both "the stream never existed" and "only call_status frames were
    published" — either way there is nothing to insert, but the stream is still
    drained for Redis hygiene."""
    stream = _FakeCallStream([])
    session = _FakeSession()
    _patch_tenant_session(monkeypatch, session)

    count = await _finalize(stream, _ref())

    assert count == 0
    assert session.executed == []  # no insert attempted for zero rows
    assert stream.cleared == [_ROOM]


@pytest.mark.asyncio
async def test_finalize_is_idempotent_on_a_redelivered_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redelivered call.ended re-runs the finalizer against the SAME stream key;
    the first pass drains + clears it, so the second pass sees an empty snapshot and
    writes nothing (in addition to the DB-level ON CONFLICT, covered in the
    integration test)."""
    stream = _FakeCallStream(
        [CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1)]
    )
    session = _FakeSession()
    _patch_tenant_session(monkeypatch, session)
    ref = _ref()

    first = await _finalize(stream, ref)
    second = await _finalize(stream, ref)

    assert (first, second) == (1, 0)
    assert len(session.executed) == 1  # only the first pass inserted


@pytest.mark.asyncio
async def test_finalize_swallows_a_raising_session_and_leaves_stream_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB failure must not propagate (closeout has already committed the call's
    terminal status) — and the stream must NOT be cleared, so its TTL backstop still
    has the data for the next redelivery / manual recovery."""
    stream = _FakeCallStream(
        [CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1)]
    )
    session = _FakeSession(error=RuntimeError("db boom"))
    _patch_tenant_session(monkeypatch, session)

    count = await _finalize(stream, _ref())

    assert count == 0
    assert stream.cleared == []


@pytest.mark.asyncio
async def test_finalize_failure_log_never_contains_exception_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SQLAlchemy statement errors embed the compiled SQL and bound parameters —
    i.e. the transcript text (PHI) — in their str(). The failure log must carry
    only the exception TYPE name, never its content or a traceback (same rule as
    agent_worker/transcript_publisher.py)."""
    sentinel = "SECRET_PHI_TOKEN"
    stream = _FakeCallStream(
        [CallStreamEvent(type="transcript", data={"role": "user", "text": sentinel}, ts=1)]
    )
    session = _FakeSession(
        error=RuntimeError(f"INSERT INTO transcript (message) VALUES ('{sentinel}')")
    )
    _patch_tenant_session(monkeypatch, session)

    with caplog.at_level("WARNING", logger=finalizer_mod.logger.name):
        count = await _finalize(stream, _ref())

    assert count == 0
    assert stream.cleared == []  # failure path — stream left for the TTL backstop
    rendered = "\n".join(
        f"{record.getMessage()}{record.exc_text or ''}" for record in caplog.records
    )
    assert sentinel not in rendered  # exception content (PHI) never logged
    assert "RuntimeError" in rendered  # the type name IS logged
    assert all(record.exc_info is None for record in caplog.records)  # no traceback attached
