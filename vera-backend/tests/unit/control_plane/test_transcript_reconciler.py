"""Transcript reconciler: recovers crash-orphaned transcript streams.

A hard worker crash (SIGKILL/OOM/eviction) never emits call.ended, so the normal
finalizer never runs and the turns expire from Redis. The reconciler sweeps live
streams and finalizes the ones that are un-persisted AND idle past the grace
window; streams whose call is already persisted are just cleared. Uses fakes for
Redis + DB seams — no external Redis/Postgres.
"""

import time
from typing import Any
from uuid import uuid4

import pytest

from control_plane.transcript_jobs import TranscriptReconciler
from vera_core.observability.correlation import RoomRef, room_name_for_call
from vera_core.transcript import InMemoryTranscriptStore, TranscriptService, transcript_stream_key

_ANCIENT_MS = 1_000  # ~1970 — always older than the idle grace window


class _FakeRedis:
    """Just the two calls the reconciler makes: SCAN keys + last-entry lookup."""

    def __init__(self, last_entry_ms: dict[str, int]) -> None:
        # keyed by room name; value is the ms component of the newest stream entry id
        self._last_entry_ms = last_entry_ms

    async def scan_iter(self, match: str, count: int) -> Any:
        for room, _ms in self._last_entry_ms.items():
            yield transcript_stream_key(room)

    async def xrevrange(self, key: str, count: int) -> list[tuple[str, dict[str, str]]]:
        room = key.removeprefix("vera:transcript:")
        ms = self._last_entry_ms.get(room)
        return [(f"{ms}-0", {})] if ms is not None else []


def _reconciler(
    redis: _FakeRedis,
    *,
    has_rows: bool,
    finalized: list[str],
    cleared: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> TranscriptReconciler:
    service = TranscriptService(InMemoryTranscriptStore())
    rec = TranscriptReconciler(
        redis,  # type: ignore[arg-type]
        sessionmaker=object(),  # type: ignore[arg-type]  # DB seams stubbed below
        transcripts=service,
        interval_seconds=300,
        idle_seconds=900,
    )

    async def _has_rows(tenant_id: Any, call_id: Any) -> bool:
        return has_rows

    async def _finalize(ref: RoomRef, room_name: str) -> int:
        finalized.append(room_name)
        return 3

    async def _clear(room_name: str) -> None:
        cleared.append(room_name)

    monkeypatch.setattr(rec, "_has_rows", _has_rows)
    monkeypatch.setattr(rec, "_finalize", _finalize)
    monkeypatch.setattr(service, "clear", _clear)
    return rec


async def test_finalizes_crash_orphaned_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    room = room_name_for_call(uuid4(), uuid4())
    finalized: list[str] = []
    cleared: list[str] = []
    rec = _reconciler(
        _FakeRedis({room: _ANCIENT_MS}),  # idle: last entry is ancient
        has_rows=False,  # never finalized (worker crashed before call.ended)
        finalized=finalized,
        cleared=cleared,
        monkeypatch=monkeypatch,
    )
    await rec.tick()
    assert finalized == [room]  # recovered
    assert cleared == [room]  # and the drained stream is reclaimed


async def test_skips_still_active_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    room = room_name_for_call(uuid4(), uuid4())
    finalized: list[str] = []
    cleared: list[str] = []
    rec = _reconciler(
        _FakeRedis({room: int(time.time() * 1000)}),  # fresh entry → call still live
        has_rows=False,
        finalized=finalized,
        cleared=cleared,
        monkeypatch=monkeypatch,
    )
    await rec.tick()
    assert finalized == []  # a live call must never be finalized early
    assert cleared == []


async def test_clears_already_finalized_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    room = room_name_for_call(uuid4(), uuid4())
    finalized: list[str] = []
    cleared: list[str] = []
    rec = _reconciler(
        _FakeRedis({room: _ANCIENT_MS}),
        has_rows=True,  # the normal finalizer already persisted this call
        finalized=finalized,
        cleared=cleared,
        monkeypatch=monkeypatch,
    )
    await rec.tick()
    assert finalized == []  # no double work
    assert cleared == [room]  # just reclaim the redundant stream


async def test_ignores_foreign_non_vera_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    finalized: list[str] = []
    cleared: list[str] = []
    # A stream key that parses to no RoomRef (some other component's stream).
    redis = _FakeRedis({"not-a-call-room": _ANCIENT_MS})
    rec = _reconciler(
        redis, has_rows=False, finalized=finalized, cleared=cleared, monkeypatch=monkeypatch
    )
    await rec.tick()
    assert finalized == []
    assert cleared == []
